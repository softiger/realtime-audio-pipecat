"""通话级会话：一次通话 = 一个 CallSession，内嵌得助 sessionId + 编排快照。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass
class CallSession:
    """
    作用域：单次 Web 通话（浏览器 WS 连接生命周期）

    call_id        本系统通话 ID
    terminal_id    得助 terminalId（设备/访客维度）
    dezhu_session  得助多轮 sessionId，空则开新会话
    phase          编排层阶段快照（由 SessionOrchestrator 回写）
    generation     打断代次快照
    """

    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    terminal_id: str = field(default_factory=lambda: f"voice-{uuid.uuid4().hex[:16]}")
    dezhu_session: str | None = None
    external_id: str | None = None
    created_at: float = field(default_factory=time)
    turn_count: int = 0
    phase: str = "init"
    generation: int = 0

    def touch_turn(self) -> None:
        self.turn_count += 1

    def bind_dezhu_session(self, session_id: str | None) -> None:
        if session_id:
            self.dezhu_session = session_id

    def sync_orchestrator(self, snap: dict[str, Any]) -> None:
        """从 SessionOrchestrator.snapshot() 同步可观测字段。"""
        if not snap:
            return
        if snap.get("phase") is not None:
            self.phase = str(snap["phase"])
        if snap.get("generation") is not None:
            try:
                self.generation = int(snap["generation"])
            except (TypeError, ValueError):
                pass
        # turn_seq 与得助 touch_turn 独立：业务回合用 turn_count
