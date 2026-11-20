"""
一轮用户发言的分段时延 stopwatch。

锚点（monotonic）：
  speech_start → speech_stop → asr_done → fc_done
  → tool_start → tool_done → response_created → first_audio → response_done

输出 ms 字典，供 bridge.latency / TRACE 展示；缺段为 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any


def _ms(t0: float | None, t1: float | None) -> int | None:
    if t0 is None or t1 is None:
        return None
    return max(0, int((t1 - t0) * 1000))


@dataclass
class TurnStopwatch:
    """单轮计时；speech_started / 新用户轮次时 reset。"""

    turn_seq: int = 0
    generation: int = 0
    t_speech_start: float | None = None
    t_speech_stop: float | None = None
    t_asr_done: float | None = None
    t_fc_done: float | None = None
    t_tool_start: float | None = None
    t_tool_done: float | None = None
    t_response_created: float | None = None
    t_first_audio: float | None = None
    t_response_done: float | None = None
    # 工具执行器回报的 duration（与 wall 可略有差）
    tool_exec_ms: int | None = None
    _first_audio_emitted: bool = field(default=False, repr=False)
    _done_emitted: bool = field(default=False, repr=False)

    def reset(self, *, generation: int = 0, turn_seq: int = 0) -> None:
        self.turn_seq = turn_seq
        self.generation = generation
        self.t_speech_start = None
        self.t_speech_stop = None
        self.t_asr_done = None
        self.t_fc_done = None
        self.t_tool_start = None
        self.t_tool_done = None
        self.t_response_created = None
        self.t_first_audio = None
        self.t_response_done = None
        self.tool_exec_ms = None
        self._first_audio_emitted = False
        self._done_emitted = False

    def mark_speech_start(self, *, generation: int, turn_seq: int | None = None) -> None:
        self.reset(generation=generation, turn_seq=turn_seq or 0)
        self.t_speech_start = monotonic()

    def mark_speech_stop(self, *, turn_seq: int, generation: int) -> None:
        self.turn_seq = turn_seq
        self.generation = generation
        self.t_speech_stop = monotonic()
        if self.t_speech_start is None:
            self.t_speech_start = self.t_speech_stop

    def mark_asr_done(self) -> None:
        if self.t_asr_done is None:
            self.t_asr_done = monotonic()

    def mark_fc_done(self) -> None:
        if self.t_fc_done is None:
            self.t_fc_done = monotonic()

    def mark_tool_start(self) -> None:
        if self.t_tool_start is None:
            self.t_tool_start = monotonic()

    def mark_tool_done(self, *, exec_ms: int | None = None) -> None:
        self.t_tool_done = monotonic()
        if exec_ms is not None:
            self.tool_exec_ms = exec_ms

    def mark_response_created(self) -> None:
        if self.t_response_created is None:
            self.t_response_created = monotonic()

    def mark_first_audio(self) -> bool:
        """首包 TTS；返回 True 表示本轮第一次。"""
        if self.t_first_audio is not None:
            return False
        self.t_first_audio = monotonic()
        return True

    def mark_response_done(self) -> None:
        if self.t_response_done is None:
            self.t_response_done = monotonic()

    def ms_dict(self) -> dict[str, int | None]:
        """
        分段毫秒：
          speak     用户开口→收尾
          asr       收尾→转写完成
          to_fc     转写完成→FC done（无 ASR 时用 speech_stop）
          tool      工具墙钟（优先 exec_ms）
          ttfb      response.created→首包；无 created 则 tool_done/speech_stop→首包
          e2e       speech_stop→首包音频
          speak_to_audio  speech_start→首包
          total     speech_stop→response.done
        """
        tool_wall = _ms(self.t_tool_start, self.t_tool_done)
        tool = self.tool_exec_ms if self.tool_exec_ms is not None else tool_wall

        asr_anchor = self.t_asr_done
        to_fc_from = asr_anchor or self.t_speech_stop

        ttfb_from = self.t_response_created or self.t_tool_done or self.t_speech_stop

        return {
            "speak": _ms(self.t_speech_start, self.t_speech_stop),
            "asr": _ms(self.t_speech_stop, self.t_asr_done),
            "to_fc": _ms(to_fc_from, self.t_fc_done),
            "tool": tool,
            "ttfb": _ms(ttfb_from, self.t_first_audio),
            "e2e": _ms(self.t_speech_stop, self.t_first_audio),
            "speak_to_audio": _ms(self.t_speech_start, self.t_first_audio),
            "total": _ms(self.t_speech_stop, self.t_response_done),
        }

    def to_event(self, *, stage: str) -> dict[str, Any]:
        return {
            "type": "bridge.latency",
            "stage": stage,
            "turn_seq": self.turn_seq,
            "generation": self.generation,
            "ms": self.ms_dict(),
        }

    def should_emit_first_audio(self) -> bool:
        if self._first_audio_emitted or self.t_first_audio is None:
            return False
        self._first_audio_emitted = True
        return True

    def should_emit_done(self) -> bool:
        if self._done_emitted or self.t_response_done is None:
            return False
        self._done_emitted = True
        return True
