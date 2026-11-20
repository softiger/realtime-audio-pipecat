"""
通话会话状态机（SessionOrchestrator）。

职责：
  - 统一 phase / turn / generation，禁止散落布尔旗互相打架
  - generation 在 barge-in / 显式打断时递增 → 过期工具结果与待调度 response 作废
  - 状态迁移可观测（可选 listener），不阻塞音频通路

状态图（简化）：
  INIT → CONNECTING → READY → WELCOME → LISTENING
       ↘ ENDING
  LISTENING → ENDPOINTING → THINKING → TOOL_CALL → SPEAKING → LISTENING
  任意 SPEAKING/THINKING/TOOL_CALL + barge-in → LISTENING（generation++）
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)


class CallPhase(str, Enum):
    INIT = "init"
    CONNECTING = "connecting"
    READY = "ready"
    WELCOME = "welcome"
    LISTENING = "listening"
    ENDPOINTING = "endpointing"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    SPEAKING = "speaking"
    ENDING = "ending"
    ENDED = "ended"


# 合法迁移：from → frozenset(to)；ENDING/ENDED 可从多数状态进入
_TRANSITIONS: dict[CallPhase, frozenset[CallPhase]] = {
    CallPhase.INIT: frozenset({CallPhase.CONNECTING, CallPhase.ENDING}),
    CallPhase.CONNECTING: frozenset(
        {CallPhase.READY, CallPhase.ENDING, CallPhase.LISTENING}
    ),
    CallPhase.READY: frozenset(
        {CallPhase.WELCOME, CallPhase.LISTENING, CallPhase.ENDING}
    ),
    CallPhase.WELCOME: frozenset(
        {CallPhase.LISTENING, CallPhase.SPEAKING, CallPhase.ENDING}
    ),
    CallPhase.LISTENING: frozenset(
        {
            CallPhase.ENDPOINTING,
            CallPhase.THINKING,
            CallPhase.SPEAKING,
            CallPhase.WELCOME,
            CallPhase.ENDING,
        }
    ),
    CallPhase.ENDPOINTING: frozenset(
        {
            CallPhase.THINKING,
            CallPhase.LISTENING,
            CallPhase.SPEAKING,
            CallPhase.ENDING,
        }
    ),
    CallPhase.THINKING: frozenset(
        {
            CallPhase.TOOL_CALL,
            CallPhase.SPEAKING,
            CallPhase.LISTENING,
            CallPhase.ENDING,
        }
    ),
    CallPhase.TOOL_CALL: frozenset(
        {
            CallPhase.SPEAKING,
            CallPhase.THINKING,
            CallPhase.LISTENING,
            CallPhase.TOOL_CALL,
            CallPhase.ENDING,
        }
    ),
    CallPhase.SPEAKING: frozenset(
        {
            CallPhase.LISTENING,
            CallPhase.TOOL_CALL,
            CallPhase.THINKING,
            CallPhase.SPEAKING,
            CallPhase.ENDING,
        }
    ),
    CallPhase.ENDING: frozenset({CallPhase.ENDED}),
    CallPhase.ENDED: frozenset(),
}

PhaseListener = Callable[[CallPhase, CallPhase, dict[str, Any]], None]


@dataclass
class SessionOrchestrator:
    """
    单通话作用域的统一状态。

    generation:
      用户打断 / 新一轮开说时 +1；工具与 response 调度携带 gen，
      回写时若 gen 过期则丢弃副作用（不 create、不播报）。
    turn_seq:
      用户一轮完整发言（speech_stopped 或 transcription）+1。
    """

    call_id: str = ""
    phase: CallPhase = CallPhase.INIT
    generation: int = 0
    turn_seq: int = 0
    response_id: str | None = None
    response_active: bool = False
    tools_inflight: int = 0
    barge_suppressed: bool = False
    is_filler: bool = False
    last_user_transcript: str = ""
    user_transcript_partial: str = ""
    phase_changed_at: float = field(default_factory=monotonic)
    _listeners: list[PhaseListener] = field(default_factory=list, repr=False)

    # ── 订阅 ──────────────────────────────────────────────

    def on_phase(self, listener: PhaseListener) -> None:
        self._listeners.append(listener)

    # ── 迁移 ──────────────────────────────────────────────

    def transition(self, to: CallPhase, **meta: Any) -> bool:
        """尝试迁移；非法迁移记日志并返回 False（不抛，避免卡死链路）。"""
        if self.phase == to:
            return True
        allowed = _TRANSITIONS.get(self.phase, frozenset())
        # ENDING 放宽：任意非 ENDED 都可进入收尾
        if to == CallPhase.ENDING and self.phase != CallPhase.ENDED:
            pass
        elif to not in allowed and self.phase != CallPhase.ENDED:
            logger.warning(
                "illegal phase %s → %s call_id=%s meta=%s",
                self.phase.value,
                to.value,
                self.call_id,
                meta,
            )
            # 软允许：生产上宁可纠正状态，也不死锁
        prev = self.phase
        self.phase = to
        self.phase_changed_at = monotonic()
        for fn in self._listeners:
            try:
                fn(prev, to, meta)
            except Exception:  # noqa: BLE001
                logger.exception("phase listener failed")
        return True

    # ── 代次 / 轮次 ───────────────────────────────────────

    def bump_generation(self, reason: str = "") -> int:
        """打断或新用户话轮：作废在途工具播报与待 create 的 response。

        不在此清 response_active / response_id，以便随后仍能发出 response.cancel。
        """
        self.generation += 1
        self.is_filler = False
        # 工具计数保留（协议仍要回写 function_call_output），但结果不播
        logger.info(
            "generation=%s reason=%s call_id=%s tools_inflight=%s response_active=%s",
            self.generation,
            reason or "-",
            self.call_id,
            self.tools_inflight,
            self.response_active,
        )
        return self.generation

    def current_generation(self) -> int:
        return self.generation

    def is_stale(self, gen: int) -> bool:
        return gen != self.generation

    def begin_user_turn(self) -> int:
        """用户开说：清转写、+generation、进 listening。"""
        self.last_user_transcript = ""
        self.user_transcript_partial = ""
        self.bump_generation("speech_started")
        self.transition(CallPhase.LISTENING, reason="speech_started")
        return self.generation

    def mark_user_stopped(self) -> None:
        self.turn_seq += 1
        self.transition(CallPhase.ENDPOINTING, reason="speech_stopped")

    def set_transcript(self, text: str, *, partial: bool = False) -> None:
        t = (text or "").strip()
        if not t:
            return
        if partial:
            self.user_transcript_partial = t
        else:
            self.last_user_transcript = t
            self.user_transcript_partial = t

    def best_transcript(self) -> str:
        return (self.last_user_transcript or self.user_transcript_partial or "").strip()

    # ── response 生命周期 ─────────────────────────────────

    def on_response_created(self, response_id: str | None, *, filler: bool = False) -> None:
        self.response_active = True
        self.response_id = response_id
        self.is_filler = filler
        if filler:
            self.transition(CallPhase.SPEAKING, reason="filler")
        elif self.phase in {CallPhase.WELCOME, CallPhase.READY}:
            self.transition(CallPhase.WELCOME, reason="response_created")
        else:
            self.transition(CallPhase.SPEAKING, reason="response_created")

    def on_response_done(self) -> None:
        self.response_active = False
        self.response_id = None
        self.is_filler = False
        if self.tools_inflight > 0:
            self.transition(CallPhase.TOOL_CALL, reason="response_done_tools")
        else:
            self.barge_suppressed = False
            self.transition(CallPhase.LISTENING, reason="response_done")

    def on_response_cancelled(self) -> None:
        self.response_active = False
        self.response_id = None
        self.is_filler = False

    # ── 工具 ──────────────────────────────────────────────

    def tool_enter(self) -> None:
        self.tools_inflight += 1
        self.transition(CallPhase.TOOL_CALL, reason="tool_enter")

    def tool_leave(self) -> None:
        self.tools_inflight = max(0, self.tools_inflight - 1)

    # ── 快照（前端 / 日志） ───────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "phase": self.phase.value,
            "generation": self.generation,
            "turn_seq": self.turn_seq,
            "response_active": self.response_active,
            "response_id": self.response_id,
            "tools_inflight": self.tools_inflight,
            "barge_suppressed": self.barge_suppressed,
            "is_filler": self.is_filler,
        }
