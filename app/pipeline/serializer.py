"""
浏览器 ↔ Pipecat 帧序列化。

上行（低时延优先）：
  - bytes  : 原始 PCM16 mono 16k（推荐，无 base64 开销）
  - text   : JSON 控制 / 兼容旧版 base64 append

下行：
  - 音频   : JSON response.audio.delta（base64 PCM24k，兼容现有播放器）
  - 控制   : bridge / tool / 转写 / 打断 等 JSON 事件
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

logger = logging.getLogger(__name__)

# 与 Qwen Realtime / 前端约定一致
BROWSER_IN_RATE = 16000
BROWSER_OUT_RATE = 24000


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _json_bytes(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class BrowserPCMSerializer(FrameSerializer):
    """科幻页 WebSocket 协议 ↔ Pipecat Frames。"""

    def __init__(self, *, call_meta: dict[str, Any] | None = None):
        super().__init__(
            params=FrameSerializer.InputParams(
                audio_in_sample_rate=BROWSER_IN_RATE,
                audio_out_sample_rate=BROWSER_OUT_RATE,
                ignore_rtvi_messages=True,
            )
        )
        self._call_meta = call_meta or {}

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if self.should_ignore_frame(frame):
            return None

        # 输出音频：尽量小 JSON，前端立刻 enqueue
        if isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
            audio = await self._resample_audio_out(frame)
            if not audio:
                return None
            return _json_bytes({"type": "response.audio.delta", "delta": _b64(audio)})

        if isinstance(frame, InterruptionFrame):
            await self._reset_audio_out()
            return _json_bytes({"type": "input_audio_buffer.speech_started"})

        if isinstance(frame, TranscriptionFrame):
            return _json_bytes(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": frame.text or "",
                }
            )

        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            msg = frame.message
            if isinstance(msg, dict):
                return _json_bytes(msg)
            if isinstance(msg, str):
                return msg
            return _json_bytes({"type": "bridge.message", "payload": str(msg)})

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        # 二进制 PCM16 → 输入帧（最低开销路径）
        if isinstance(data, (bytes, bytearray)):
            pcm = bytes(data)
            if not pcm:
                return None
            pcm = await self._resample_audio_in(pcm)
            return InputAudioRawFrame(
                audio=pcm,
                sample_rate=self._sample_rate or BROWSER_IN_RATE,
                num_channels=1,
            )

        text = data if isinstance(data, str) else data.decode("utf-8", errors="ignore")
        if not text:
            return None

        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return None

        if not isinstance(event, dict):
            return None

        et = event.get("type")

        if et == "input_audio_buffer.append":
            b64 = event.get("audio") or ""
            if not b64:
                return None
            try:
                pcm = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                return None
            pcm = await self._resample_audio_in(pcm)
            return InputAudioRawFrame(
                audio=pcm,
                sample_rate=self._sample_rate or BROWSER_IN_RATE,
                num_channels=1,
            )

        # 其余控制面：交给管线里的控制处理器
        return InputTransportMessageFrame(message=event)
