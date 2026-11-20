"""
通话级 Pipecat Pipeline 组装。

数据流（全双工、低时延）：

  Browser WS
      │  binary PCM16@16k / JSON ctrl
      ▼
  FastAPIWebsocketTransport.input
      │  InputAudioRawFrame
      ▼
  QwenRealtimeService  ──WS──►  qwen-audio realtime (ASR/VAD/TTS/FC)
      │  tool: query_business_bot
      │            │
      │            └──► DezhuChatClient (/chat/reply, sessionId 多轮)
      │
      │  TTSAudioRawFrame / OutputTransportMessage
      ▼
  FastAPIWebsocketTransport.output
      │  JSON audio delta + UI events
      ▼
  Browser 播放 24k PCM
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from pipecat.frames.frames import EndFrame, OutputTransportMessageUrgentFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

from app.config import Settings
from app.dezhu.client import DezhuChatClient, extract_speakable_text
from app.dezhu.session import CallSession
from app.pipeline.qwen_realtime import QwenRealtimeService
from app.pipeline.serializer import BROWSER_IN_RATE, BROWSER_OUT_RATE, BrowserPCMSerializer
from app.pipeline.state_machine import SessionOrchestrator
from app.pipeline.tool_args import parse_tool_question

logger = logging.getLogger(__name__)


class CallMetaInjector(FrameProcessor):
    """给浏览器事件补上 call_id / dezhu_session / turn_count / phase。"""

    def __init__(
        self,
        call: CallSession,
        orch: SessionOrchestrator | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.call = call
        self.orch = orch

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputTransportMessageUrgentFrame) and isinstance(
            frame.message, dict
        ):
            msg = dict(frame.message)
            msg.setdefault("call_id", self.call.call_id)
            if self.call.dezhu_session:
                msg.setdefault("dezhu_session", self.call.dezhu_session)
            msg.setdefault("turn_count", self.call.turn_count)
            if self.orch is not None:
                snap = self.orch.snapshot()
                self.call.sync_orchestrator(snap)
                msg.setdefault("phase", snap.get("phase"))
                msg.setdefault("generation", snap.get("generation"))
            else:
                msg.setdefault("phase", self.call.phase)
                msg.setdefault("generation", self.call.generation)
            if msg.get("type") == "bridge.status" and msg.get("status") == "connected":
                msg.setdefault("terminal_id", self.call.terminal_id)
            frame = OutputTransportMessageUrgentFrame(message=msg)
        await self.push_frame(frame, direction)


async def run_voice_bot(
    websocket: WebSocket,
    *,
    settings: Settings,
    dezhu: DezhuChatClient,
    call: CallSession | None = None,
) -> None:
    """单次通话：建 Pipeline → 跑到断开。"""
    call = call or CallSession()

    if not settings.has_api_key:
        await websocket.send_json(
            {
                "type": "error",
                "message": "未配置 DASHSCOPE_API_KEY，请在 .env 中填写后重启。",
                "call_id": call.call_id,
            }
        )
        return
    if not settings.has_dezhu:
        await websocket.send_json(
            {
                "type": "error",
                "message": "未配置得助参数（DZ_APP_ID / DZ_CHANNEL_ID / DZ_PRIVATE_KEY）。",
                "call_id": call.call_id,
            }
        )
        return

    qwen_holder: dict[str, QwenRealtimeService] = {}
    # 通话级统一状态：phase / generation / turn
    orch = SessionOrchestrator(call_id=call.call_id)
    call.sync_orchestrator(orch.snapshot())

    async def tool_handler(name: str, arguments: Any) -> str:
        if name != "query_business_bot":
            return f"未知工具: {name}"
        # question 已在 QwenRealtimeService 用本轮 ASR 校准
        question = parse_tool_question(arguments)
        if not question:
            return "未识别到用户问题，请让用户再说一次。"
        text, _raw = await dezhu.chat(question, call)
        return text

    async def on_ready() -> None:
        """session 就绪后：得助 WELCOME → 语音播报（带超时，不卡死 READY）。"""
        svc = qwen_holder.get("svc")
        if svc is None:
            return
        try:
            # 欢迎语最长 1.5s，超时本地兜底，避免业务慢阻塞接听
            try:
                raw = await asyncio.wait_for(dezhu.welcome(call), timeout=1.5)
                text = (
                    extract_speakable_text(raw)
                    or "您好，我是智能语音客服，有什么可以帮您？"
                )
            except asyncio.TimeoutError:
                logger.warning("welcome timeout call_id=%s", call.call_id)
                text = "您好，我是智能语音客服，有什么可以帮您？"
            await svc.push_frame(
                OutputTransportMessageUrgentFrame(
                    message={
                        "type": "dezhu.welcome",
                        "text": text,
                        "session_id": call.dezhu_session,
                        "call_id": call.call_id,
                    }
                )
            )
            await svc.speak_text(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("welcome failed")
            await svc.push_frame(
                OutputTransportMessageUrgentFrame(
                    message={"type": "error", "message": f"欢迎语获取失败: {exc}"}
                )
            )
            try:
                await svc.speak_text("您好，语音客服已上线，请直接说出您的问题。")
            except Exception:  # noqa: BLE001
                pass

    qwen = QwenRealtimeService(
        settings=settings,
        tool_handler=tool_handler,
        on_ready=on_ready,
        orchestrator=orch,
        call_id=call.call_id,
    )
    qwen_holder["svc"] = qwen
    meta = CallMetaInjector(call, orch=orch)

    serializer = BrowserPCMSerializer(
        call_meta={"call_id": call.call_id, "terminal_id": call.terminal_id}
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            # 入：浏览器 16k；出：模型 24k，由 serializer 负责 wire 侧
            audio_in_sample_rate=BROWSER_IN_RATE,
            audio_out_sample_rate=BROWSER_OUT_RATE,
            audio_in_enabled=True,
            audio_out_enabled=True,
            # 更小出块 → 首包更快触达浏览器（约 20ms@24k mono s16）
            audio_out_10ms_chunks=2,
            add_wav_header=False,
            serializer=serializer,
            session_timeout=None,
            # 浏览器同源；空列表=不限制
            allowed_origins=[],
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            qwen,
            meta,
            transport.output(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=False,
            enable_usage_metrics=False,
            # 管线时钟与浏览器采集对齐
            audio_in_sample_rate=BROWSER_IN_RATE,
            audio_out_sample_rate=BROWSER_OUT_RATE,
        ),
        enable_rtvi=False,
        enable_turn_tracking=False,
        # 长通话不因静默空闲被踢；断开由 transport 事件收尾
        idle_timeout_secs=None,
        cancel_on_idle_timeout=False,
    )

    @transport.event_handler("on_client_disconnected")
    async def _on_disc(_transport, _ws):  # noqa: ANN001
        await task.queue_frame(EndFrame())

    runner = WorkerRunner(handle_sigint=False)
    try:
        await runner.add_workers(task)
        await runner.run()
    finally:
        call.sync_orchestrator(orch.snapshot())
        logger.info(
            "call ended id=%s dezhu_session=%s turns=%s phase=%s gen=%s",
            call.call_id,
            call.dezhu_session,
            call.turn_count,
            call.phase,
            call.generation,
        )
