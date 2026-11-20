"""
Qwen Audio Realtime（百炼 OpenAI-Realtime 兼容协议）→ Pipecat FrameProcessor。

职责：
  上行：InputAudioRawFrame / 控制帧 → DashScope WS（持续接收，不因业务阻塞）
  下行：server events → TTS / 打断 / 转写 / FunctionCall / 浏览器 JSON

稳定性（复杂场景）：
  - SessionOrchestrator：统一 phase / generation，避免状态死锁
  - ResponseScheduler：response.create 单飞 + 排队 + 打断作废
  - ToolExecutor：业务 API 超时/幂等/代次作废，不阻塞音频
  - speech_started → generation++ → 取消在途回复，防重复播报
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import TextType

from app.config import Settings
from app.pipeline.latency import TurnStopwatch
from app.pipeline.response_scheduler import ResponseScheduler
from app.pipeline.session_config import build_session_update
from app.pipeline.state_machine import CallPhase, SessionOrchestrator
from app.pipeline.tool_args import with_calibrated_question
from app.tools.executor import ToolExecutor, ToolPolicy

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, Any], Awaitable[str]]

QWEN_OUT_RATE = 24000
QWEN_IN_RATE = 16000


def _event_id() -> str:
    return f"event_{int(time.time() * 1000)}"


def _normalize_duplex(mode: str) -> str:
    m = (mode or "soft").strip().lower()
    if m in {"full", "gate", "half", "soft"}:
        return m
    return "soft"


class QwenRealtimeService(FrameProcessor):
    """全双工 S2S：浏览器音频进、模型音频出，工具走得助。"""

    def __init__(
        self,
        *,
        settings: Settings,
        tool_handler: ToolHandler | None = None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        orchestrator: SessionOrchestrator | None = None,
        call_id: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.settings = settings
        self._tool_handler = tool_handler
        self._on_ready = on_ready
        self._duplex = _normalize_duplex(getattr(settings, "voice_duplex", "soft"))

        self._ws: Any | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False

        # ── 统一会话状态 ──────────────────────────────────
        self.orch = orchestrator or SessionOrchestrator(call_id=call_id or "")
        if call_id and not self.orch.call_id:
            self.orch.call_id = call_id
        self.orch.on_phase(self._on_phase_change)

        # FC 聚合（协议层）
        self._fc_args: dict[str, str] = {}
        self._fc_names: dict[str, str] = {}
        self._fc_done: set[str] = set()
        self._fc_lock = asyncio.Lock()
        self._audio_started = False

        # 工具等待垫词
        self._filler_task: asyncio.Task | None = None

        # 响应调度：create/cancel 唯一入口
        self._scheduler = ResponseScheduler(
            send=self._send_event,
            is_response_active=lambda: self.orch.response_active,
            get_generation=self.orch.current_generation,
            settle_ms=int(getattr(settings, "response_settle_ms", 50) or 50),
        )

        # 业务工具：超时 + 幂等 + 代次
        self._executor = ToolExecutor(
            handler=tool_handler,
            policy=ToolPolicy(
                timeout_ms=int(getattr(settings, "tool_timeout_ms", 8000) or 8000),
                fallback_text=(
                    getattr(settings, "tool_fallback_text", None)
                    or "查询有点慢，请稍后再试，或换个方式描述您的问题。"
                ),
            ),
            get_generation=self.orch.current_generation,
        )

        # 用户一轮分段时延（speech_stop → 首包 TTS 等）
        self._latency = TurnStopwatch()

    # ── 可观测 ────────────────────────────────────────────

    def _on_phase_change(self, prev, to, meta: dict) -> None:  # noqa: ANN001
        logger.debug(
            "phase %s → %s call_id=%s gen=%s meta=%s",
            prev.value if hasattr(prev, "value") else prev,
            to.value if hasattr(to, "value") else to,
            self.orch.call_id,
            self.orch.generation,
            meta,
        )

    async def _emit_phase(self) -> None:
        """前端兼容 phase 名：tool_wait / speaking / filler / idle。"""
        snap = self.orch.snapshot()
        raw = snap["phase"]
        if raw == CallPhase.TOOL_CALL.value or self.orch.tools_inflight > 0:
            ui_phase = "tool_wait"
        elif raw == CallPhase.SPEAKING.value:
            ui_phase = "filler" if self.orch.is_filler else "speaking"
        elif raw in {
            CallPhase.WELCOME.value,
        }:
            ui_phase = "speaking"
        elif raw in {
            CallPhase.THINKING.value,
            CallPhase.ENDPOINTING.value,
        }:
            ui_phase = "thinking"
        else:
            ui_phase = "idle"
        await self._emit_urgent(
            {
                "type": "bridge.phase",
                "phase": ui_phase,
                "call_phase": raw,
                "generation": snap["generation"],
                "turn_seq": snap["turn_seq"],
                "tools_inflight": snap["tools_inflight"],
            }
        )

    async def _emit_latency(self, stage: str) -> None:
        """仅在有用户收尾锚点时推送，避免欢迎语/垫词污染。"""
        if self._latency.t_speech_stop is None:
            return
        await self._emit(self._latency.to_event(stage=stage))

    # ── lifecycle ──────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            await self._connect()
            return

        if isinstance(frame, (EndFrame, CancelFrame)):
            self.orch.transition(CallPhase.ENDING, reason="end_frame")
            await self._disconnect()
            self.orch.transition(CallPhase.ENDED, reason="disconnected")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InputAudioRawFrame):
            # 持续上行：工具/推理状态绝不阻塞此路径（仅 half 播报门控）
            await self._send_audio(frame.audio)
            return

        if isinstance(frame, InputTransportMessageFrame):
            await self._handle_client_message(frame.message)
            return

        if isinstance(frame, InterruptionFrame):
            if direction == FrameDirection.UPSTREAM:
                await self._handle_barge_in(reason="upstream_interrupt", explicit=True)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self._disconnect()
        await super().cleanup()

    # ── upstream connection ────────────────────────────────

    async def _connect(self) -> None:
        if not self.settings.has_api_key:
            await self._emit_urgent(
                {"type": "error", "message": "未配置 DASHSCOPE_API_KEY，请在 .env 中填写后重启。"}
            )
            return

        self.orch.transition(CallPhase.CONNECTING, reason="connect")

        try:
            from websockets.asyncio.client import connect as websocket_connect
        except ImportError as exc:
            raise RuntimeError("需要 websockets>=13 依赖") from exc

        url = self.settings.realtime_ws_url()
        headers = {
            "Authorization": f"Bearer {self.settings.dashscope_api_key}",
            "x-dashscope-dataInspection": "disable",
        }
        ws_id = (self.settings.dashscope_workspace_id or "").strip()
        if ws_id:
            headers["X-DashScope-WorkSpace"] = ws_id

        await self._emit_urgent(
            {
                "type": "bridge.status",
                "status": "connecting",
                "url": url.split("?")[0],
                "model": self.settings.realtime_model,
            }
        )

        try:
            self._ws = await websocket_connect(
                url,
                additional_headers=headers,
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Qwen Realtime connect failed")
            await self._emit_urgent({"type": "error", "message": f"上游连接失败: {exc}"})
            self.orch.transition(CallPhase.ENDING, reason="connect_fail")
            return

        self._closed = False
        await self._send_event(build_session_update(self.settings))
        self._recv_task = self.create_task(self._recv_loop(), name="qwen-realtime-recv")

    async def _disconnect(self) -> None:
        self._closed = True
        self._scheduler.on_interrupted()
        task = self._recv_task
        self._recv_task = None
        if task is not None:
            try:
                await self.cancel_task(task)
            except Exception:  # noqa: BLE001
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass
        ft = self._filler_task
        self._filler_task = None
        if ft and not ft.done():
            ft.cancel()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ── send helpers ───────────────────────────────────────

    async def _send_event(self, event: dict[str, Any]) -> None:
        if self._ws is None or self._closed:
            return
        payload = dict(event)
        payload.setdefault("event_id", _event_id())
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    def _playback_blocks_uplink(self) -> bool:
        return self._duplex == "half" and (
            self._audio_started
            or self.orch.barge_suppressed
            or self.orch.is_filler
        )

    def _playback_blocks_barge_in(self) -> bool:
        if self._duplex not in {"gate", "half"}:
            return False
        return bool(self._audio_started or self.orch.is_filler)

    async def _send_audio(self, pcm: bytes) -> None:
        if not pcm or self._ws is None or self._closed:
            return
        if self._playback_blocks_uplink():
            return
        await self._send_event(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def _handle_barge_in(self, *, reason: str, explicit: bool = False) -> bool:
        """
        打断：generation++、清 pending create、cancel 上游。
        explicit=True：用户/客户端强制，即使开场白也 cancel。
        """
        # 开场白回采：非显式打断直接忽略（不 bump，避免误杀后续工具）
        if not explicit and self.orch.barge_suppressed and not self.orch.is_filler:
            logger.debug("barge-in ignored (suppressed) reason=%s", reason)
            return False

        self.orch.bump_generation(reason)
        self._scheduler.on_interrupted()
        self._audio_started = False

        if explicit:
            self.orch.barge_suppressed = False
            self.orch.is_filler = False

        # 只发 cancel，不立刻清 response_active：
        # 上游仍占坑，需等 response.done 再 idle，否则垫词/工具 create 会撞
        # "Cannot create response while another response is in progress"
        cancelled = await self._scheduler.cancel_active(response_id=self.orch.response_id)
        if not cancelled and not self.orch.response_active:
            self._scheduler.on_response_finished()
        return cancelled

    async def _emit_urgent(self, message: dict[str, Any]) -> None:
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))

    async def _emit(self, message: dict[str, Any]) -> None:
        await self.push_frame(OutputTransportMessageUrgentFrame(message=message))

    # ── client control ─────────────────────────────────────

    async def _handle_client_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        et = message.get("type")
        if et in {
            "input_audio_buffer.commit",
            "input_audio_buffer.clear",
            "response.create",
            "response.cancel",
            "session.update",
        }:
            await self._send_event(message)
        elif et == "client.ping":
            await self._emit({"type": "client.pong", "t": message.get("t")})
        elif et == "client.close":
            await self.push_frame(EndFrame())
        elif et == "client.interrupt":
            await self._handle_barge_in(reason="client.interrupt", explicit=True)
            await self.push_frame(InterruptionFrame())
        elif et == "client.user_text":
            # 自动化测试 / 文本旁路：无麦注入标准问题，仍走模型+工具+TTS
            text = str(message.get("text") or message.get("ask") or "").strip()
            if text:
                await self._inject_user_text(text)

    async def _inject_user_text(self, text: str) -> None:
        """
        模拟「用户说完一句」：
          begin_user_turn → speech_stop 锚点 → 转写就绪 → item.create → response.create
        时延：asr≈0（无 VAD）；e2e/tool/ttfb 仍有效。
        """
        if self._ws is None or self._closed:
            return
        safe = (text or "").strip()
        if not safe:
            return

        # 允许打断欢迎语/在途播报
        self.orch.barge_suppressed = False
        self.orch.is_filler = False
        self.orch.begin_user_turn()
        gen = self.orch.current_generation()
        self._scheduler.on_interrupted()
        self._audio_started = False
        self._latency.mark_speech_start(generation=gen, turn_seq=self.orch.turn_seq)

        cancelled = await self._scheduler.cancel_active(
            response_id=self.orch.response_id
        )
        if not cancelled and not self.orch.response_active:
            self._scheduler.on_response_finished()

        await self.push_frame(InterruptionFrame())
        await self.push_frame(UserStartedSpeakingFrame())

        self.orch.mark_user_stopped()
        self.orch.transition(CallPhase.THINKING, reason="client.user_text")
        self._latency.mark_speech_stop(
            turn_seq=self.orch.turn_seq,
            generation=gen,
        )
        self.orch.set_transcript(safe, partial=False)
        self._latency.mark_asr_done()
        await self.push_frame(UserStoppedSpeakingFrame())

        await self._emit(
            {
                "type": "bridge.user_text",
                "text": safe,
                "turn_seq": self.orch.turn_seq,
                "generation": gen,
            }
        )
        await self._emit(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": safe,
                "source": "client.user_text",
            }
        )
        await self._emit_phase()

        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": safe}],
                },
            }
        )
        await self._scheduler.request_create(
            generation=gen,
            modalities=["text", "audio"],
            tool_choice="auto",
        )

    # ── public speak API ───────────────────────────────────

    async def speak_text(
        self,
        text: str,
        *,
        tool_choice: str = "none",
        as_filler: bool = False,
        prompt_user: str = "（系统）请播报开场白。",
    ) -> None:
        safe = (text or "").strip()
        if not safe or self._ws is None:
            return
        # busy = 上游 active / create·cancel 在途；垫词与开场白都不可硬闯
        if self._scheduler.is_busy():
            logger.info(
                "speak_text deferred/skip busy filler=%s active=%s",
                as_filler,
                self.orch.response_active,
            )
            if as_filler:
                return
            # 开场白：若仍 busy 则排队，等 done 后单飞
        gen = self.orch.current_generation()
        self.orch.barge_suppressed = True
        self.orch.is_filler = bool(as_filler)
        if not as_filler:
            self.orch.transition(CallPhase.WELCOME, reason="speak_text")

        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt_user}],
                },
            }
        )
        # 经 scheduler：busy 时入队，idle 时单飞，避免打断后与 cancel 竞态
        await self._scheduler.request_create(
            generation=gen,
            modalities=["text", "audio"],
            tool_choice=tool_choice,
            instructions=(
                "请用自然亲切的中文口语完整朗读下面内容，"
                "不要增删改写，不要反问，不要调用任何工具：\n" + safe
            ),
        )

    async def _speak_tool_filler(self) -> None:
        text = (self.settings.tool_filler_text or "").strip() or "稍等，我帮您查一下。"
        await self._emit(
            {
                "type": "bridge.phase",
                "phase": "tool_wait",
                "filler": text,
            }
        )
        await self.speak_text(
            text,
            tool_choice="none",
            as_filler=True,
            prompt_user="（系统）请播报等待提示。",
        )

    async def _tool_filler_loop(self, generation: int) -> None:
        if not getattr(self.settings, "tool_filler_enabled", True):
            return
        delay = max(0, int(getattr(self.settings, "tool_filler_delay_ms", 450) or 0)) / 1000.0
        try:
            await asyncio.sleep(delay)
            if self.orch.is_stale(generation) or self.orch.tools_inflight <= 0 or self._closed:
                return
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if self.orch.is_stale(generation) or self.orch.tools_inflight <= 0 or self._closed:
                    return
                if not self._scheduler.is_busy():
                    break
                await asyncio.sleep(0.05)
            if (
                self.orch.is_stale(generation)
                or self.orch.tools_inflight <= 0
                or self._scheduler.is_busy()
                or self._closed
            ):
                return
            await self._speak_tool_filler()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("tool filler failed")

    async def _await_user_transcript(self) -> str:
        wait_ms = int(getattr(self.settings, "tool_transcript_wait_ms", 550) or 0)
        if wait_ms <= 0:
            return self.orch.best_transcript()
        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            t = (self.orch.last_user_transcript or "").strip()
            if t:
                return t
            await asyncio.sleep(0.04)
        return self.orch.best_transcript()

    async def wait_session_ready(self, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.orch.phase not in {CallPhase.INIT, CallPhase.CONNECTING}:
                return True
            await asyncio.sleep(0.05)
        return self.orch.phase not in {CallPhase.INIT, CallPhase.CONNECTING}

    # ── receive loop ───────────────────────────────────────

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                try:
                    await self._handle_server_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Qwen event handle failed type=%s", event.get("type")
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            if not self._closed:
                logger.exception("Qwen Realtime recv ended")
                await self._emit({"type": "bridge.status", "status": "disconnected"})
        finally:
            self._closed = True

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        et = event.get("type") or ""

        if et == "session.created" or et == "session.updated":
            if self.orch.phase in {CallPhase.INIT, CallPhase.CONNECTING}:
                self.orch.transition(CallPhase.READY, reason=et)
                await self._emit(
                    {
                        "type": "bridge.status",
                        "status": "connected",
                        "model": self.settings.realtime_model,
                        "voice": self.settings.realtime_voice,
                        "turn_detection": self.settings.turn_detection,
                        "voice_duplex": self._duplex,
                        "call_id": self.orch.call_id,
                    }
                )
                if self._on_ready:
                    self.create_task(self._safe_on_ready(), name="qwen-on-ready")
            return

        if et == "error":
            err = event.get("error") or event
            msg = ""
            code = ""
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("msg") or "")
                code = str(err.get("code") or err.get("type") or "")
            else:
                msg = str(err or "")
            if not msg:
                msg = str(event.get("message") or "upstream error")
            blob = f"{msg} {code}".lower()
            # cancel 时无 active：本地对齐 idle 并 flush
            benign_cancel = (
                "no active response" in blob
                or "not active" in blob
                or "cancellation_failed" in blob
                or ("cancel" in blob and "response" in blob)
            )
            # create 撞上仍在途的 response：保持 busy，等 done 再 flush
            in_progress = (
                "another response" in blob
                or "response is in progress" in blob
                or "already in progress" in blob
            )
            if benign_cancel:
                self.orch.on_response_cancelled()
                self._scheduler.on_response_finished()
                await self._scheduler.flush_if_idle()
                logger.info("Qwen benign error ignored: %s code=%s", msg, code)
                return
            if in_progress:
                # 本地 create 被拒：清 create_inflight，但认为上游仍占用
                self._scheduler.on_create_rejected()
                if not self.orch.response_active:
                    self.orch.response_active = True
                logger.warning(
                    "Qwen create rejected (in progress), wait done: %s", msg
                )
                return
            await self._emit({"type": "error", "message": msg})
            return

        # 用户开说 → 打断本地播放 + 取消机器人
        if et == "input_audio_buffer.speech_started":
            if self.orch.barge_suppressed and not self.orch.is_filler:
                logger.debug("speech_started ignored while barge-in suppressed")
                return
            if self._playback_blocks_barge_in():
                logger.debug(
                    "speech_started ignored: duplex=%s during playback", self._duplex
                )
                return
            self.orch.begin_user_turn()
            self._scheduler.on_interrupted()
            self._audio_started = False
            self._latency.mark_speech_start(
                generation=self.orch.current_generation(),
                turn_seq=self.orch.turn_seq,
            )
            await self.push_frame(InterruptionFrame())
            await self.push_frame(UserStartedSpeakingFrame())
            # 同 _handle_barge_in：cancel 后保持 busy，等 response.done
            cancelled = await self._scheduler.cancel_active(
                response_id=self.orch.response_id
            )
            if not cancelled and not self.orch.response_active:
                self._scheduler.on_response_finished()
            await self._emit(event)
            await self._emit_phase()
            return

        if et == "input_audio_buffer.speech_stopped":
            self.orch.mark_user_stopped()
            self.orch.transition(CallPhase.THINKING, reason="speech_stopped")
            self._latency.mark_speech_stop(
                turn_seq=self.orch.turn_seq,
                generation=self.orch.current_generation(),
            )
            await self.push_frame(UserStoppedSpeakingFrame())
            await self._emit(event)
            await self._emit_phase()
            return

        if et == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript") or ""
            if transcript:
                self.orch.set_transcript(str(transcript), partial=False)
                self._latency.mark_asr_done()
                await self.push_frame(
                    TranscriptionFrame(
                        text=transcript,
                        user_id="",
                        timestamp=str(time.time()),
                    )
                )
            await self._emit(event)
            return

        if et in {
            "conversation.item.input_audio_transcription.delta",
            "response.audio_transcript.delta",
            "response.text.delta",
            "response.audio_transcript.done",
            "response.done",
            "response.created",
            "response.output_item.added",
        }:
            if et == "conversation.item.input_audio_transcription.delta":
                piece = (
                    event.get("delta")
                    or event.get("text")
                    or event.get("transcript")
                    or ""
                )
                if piece:
                    if event.get("transcript"):
                        self.orch.set_transcript(
                            str(event.get("transcript") or ""), partial=True
                        )
                    else:
                        prev = self.orch.user_transcript_partial
                        self.orch.set_transcript(
                            (prev + str(piece)).strip(), partial=True
                        )

            if et == "response.created":
                resp = event.get("response") if isinstance(event.get("response"), dict) else {}
                rid = (
                    (resp or {}).get("id")
                    or event.get("response_id")
                    or event.get("id")
                )
                self.orch.on_response_created(rid, filler=self.orch.is_filler)
                self._scheduler.on_response_created(rid)
                self._audio_started = False
                # 垫词不计入正式应答 TTFB
                if not self.orch.is_filler:
                    self._latency.mark_response_created()
                await self.push_frame(LLMFullResponseStartFrame())
                await self._emit_phase()

            if et == "response.done":
                was_filler = self.orch.is_filler
                if self._audio_started:
                    await self.push_frame(TTSStoppedFrame())
                    self._audio_started = False
                await self.push_frame(LLMFullResponseEndFrame())
                self.orch.on_response_done()
                self._scheduler.on_response_finished()
                # 纯 FC 的 done 不收口 total：等正式首包播报后再记 response_done
                if (
                    not was_filler
                    and self._latency.t_speech_stop is not None
                    and self._latency.t_first_audio is not None
                ):
                    self._latency.mark_response_done()
                    if self._latency.should_emit_done():
                        await self._emit_latency("response_done")
                await self._emit_phase()
                # 工具结果已回写且排队中的 create：空闲后单飞发送
                await self._scheduler.flush_if_idle()

            if et in {"response.audio_transcript.delta", "response.text.delta"}:
                delta = event.get("delta") or ""
                if delta:
                    await self.push_frame(
                        TTSTextFrame(text=delta, text_type=TextType.TOKEN)
                    )

            if et == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = item.get("call_id") or item.get("id") or ""
                    name = item.get("name") or ""
                    if call_id and name:
                        self._fc_names[call_id] = name
                        self._fc_args.setdefault(call_id, item.get("arguments") or "")

            await self._emit(event)
            return

        if et == "response.audio.delta":
            b64 = event.get("delta") or ""
            if not b64:
                return
            try:
                pcm = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                return
            if not self._audio_started:
                self._audio_started = True
                await self.push_frame(TTSStartedFrame())
                # 正式应答首包：E2E / TTFB 主观测点
                if not self.orch.is_filler and self._latency.mark_first_audio():
                    if self._latency.should_emit_first_audio():
                        await self._emit_latency("first_audio")
            await self.push_frame(
                TTSAudioRawFrame(audio=pcm, sample_rate=QWEN_OUT_RATE, num_channels=1)
            )
            return

        if et == "response.function_call_arguments.delta":
            call_id = event.get("call_id") or event.get("item_id") or ""
            delta = event.get("delta") or ""
            if call_id:
                self._fc_args[call_id] = self._fc_args.get(call_id, "") + delta
                name = event.get("name")
                if name:
                    self._fc_names[call_id] = name
            return

        if et in {"response.function_call_arguments.done", "response.function_call.done"}:
            call_id = event.get("call_id") or event.get("item_id") or ""
            name = event.get("name") or self._fc_names.get(call_id) or ""
            arguments = event.get("arguments")
            if arguments is None:
                arguments = self._fc_args.get(call_id, "{}")
            self._latency.mark_fc_done()
            # 异步跑工具，不阻塞 recv 循环 → 音频/事件持续处理
            self.create_task(
                self._run_tool_once(call_id, name, arguments),
                name=f"tool-{call_id[:12] or 'x'}",
            )
            return

        if et == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call" and item.get("status") == "completed":
                call_id = item.get("call_id") or item.get("id") or ""
                name = item.get("name") or self._fc_names.get(call_id) or ""
                arguments = item.get("arguments") or self._fc_args.get(call_id, "{}")
                self._latency.mark_fc_done()
                self.create_task(
                    self._run_tool_once(call_id, name, arguments),
                    name=f"tool-{call_id[:12] or 'x'}",
                )
                return
            await self._emit(event)
            return

        if et.startswith("response.") or et.startswith("conversation.") or et.startswith(
            "input_"
        ):
            await self._emit(event)

    async def _safe_on_ready(self) -> None:
        try:
            if self._on_ready:
                await self._on_ready()
        except Exception:  # noqa: BLE001
            logger.exception("on_ready failed")

    # ── tools ──────────────────────────────────────────────

    async def _run_tool_once(self, call_id: str, name: str, arguments: Any) -> None:
        if not call_id or not name:
            return
        async with self._fc_lock:
            if call_id in self._fc_done:
                return
            self._fc_done.add(call_id)

        gen = self.orch.current_generation()
        self.orch.tool_enter()
        await self._emit_phase()

        transcript = await self._await_user_transcript()
        # 打断后转写可能已清，仍用发起时缓存的 arguments 校准
        calibrated = with_calibrated_question(arguments, transcript)

        self._latency.mark_tool_start()
        await self._emit(
            {
                "type": "tool.started",
                "name": name,
                "call_id": call_id,
                "arguments": calibrated,
                "transcript": transcript,
                "calibrated": calibrated != arguments,
                "generation": gen,
            }
        )

        if self._filler_task and not self._filler_task.done():
            self._filler_task.cancel()
            try:
                await self._filler_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._filler_task = self.create_task(
            self._tool_filler_loop(gen), name="tool-filler"
        )

        try:
            result = await self._executor.execute(
                name=name,
                arguments=calibrated,
                fc_call_id=call_id,
                session_call_id=self.orch.call_id,
                generation=gen,
            )
        finally:
            self.orch.tool_leave()
            ft = self._filler_task
            self._filler_task = None
            if ft and not ft.done():
                ft.cancel()
                try:
                    await ft
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        self._latency.mark_tool_done(exec_ms=result.duration_ms)
        await self._emit(
            {
                "type": "tool.finished",
                "name": name,
                "call_id": call_id,
                "result": result.text,
                "ok": result.ok,
                "timed_out": result.timed_out,
                "stale": result.stale,
                "cached": result.cached,
                "generation": gen,
                "duration_ms": result.duration_ms,
            }
        )
        await self._emit_latency("tool_done")

        # 协议要求：无论是否 stale，都必须回写 function_call_output
        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result.text,
                },
            }
        )

        # 代次过期（用户已打断）：不 create，避免抢话/重复
        if result.stale or self.orch.is_stale(gen):
            logger.info(
                "tool result stale skip response.create call_id=%s gen=%s",
                call_id,
                gen,
            )
            await self._emit_phase()
        else:
            await self._scheduler.after_tool_ready(
                generation=gen,
                cancel_filler=True,
                is_filler_playing=lambda: self.orch.is_filler and self._scheduler.is_busy(),
            )

        self._fc_args.pop(call_id, None)
        self._fc_names.pop(call_id, None)
        await self._emit_phase()
