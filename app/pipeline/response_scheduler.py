"""
响应任务可控调度。

问题背景：
  - 工具返回、垫词播完、response.done 都可能触发 response.create
  - 无 active response 时 cancel 会协议报错；有 active 时连发 create 会冲突
  - 用户打断后，过期的「工具后 create」不能再发
  - cancel 发出后上游仍占坑，本地若立刻当 idle 再 create →
    "Cannot create response while another response is in progress"

策略：
  - 单飞：同一时刻最多一个 in-flight create 意图
  - 代次：schedule 时绑定 generation，fire 前校验
  - 门闩：busy（active / create_inflight / cancel_inflight）时只置 pending
  - 打断：bump generation + clear pending；cancel 后保持 busy 直到 done
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SendEvent = Callable[[dict[str, Any]], Awaitable[None]]
IsResponseActive = Callable[[], bool]
GetGeneration = Callable[[], int]


@dataclass
class _PendingCreate:
    generation: int
    modalities: list[str] = field(default_factory=lambda: ["text", "audio"])
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponseScheduler:
    """
    调度 response.create / response.cancel。

    不持有 WebSocket；通过注入的 send / 状态回调解耦。
    """

    send: SendEvent
    is_response_active: IsResponseActive
    get_generation: GetGeneration
    # cancel 后短暂等待，降低 create 紧贴 cancel 的竞态
    settle_ms: int = 50

    _pending: _PendingCreate | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _cancel_inflight: bool = field(default=False, repr=False)
    _create_inflight: bool = field(default=False, repr=False)
    _last_response_id: str | None = field(default=None, repr=False)

    def note_response_id(self, rid: str | None) -> None:
        self._last_response_id = rid

    def clear_response_id(self) -> None:
        self._last_response_id = None
        self._cancel_inflight = False
        self._create_inflight = False

    def is_busy(self) -> bool:
        """上游占用或本地 create/cancel 在途：不可再发 create。"""
        return (
            self.is_response_active()
            or self._cancel_inflight
            or self._create_inflight
        )

    async def request_create(
        self,
        *,
        generation: int | None = None,
        modalities: list[str] | None = None,
        force_queue: bool = False,
        **response_extra: Any,
    ) -> bool:
        """
        请求拉起一轮模型回复。

        返回 True 表示已立即发送或已入队；False 表示代次过期被丢弃。
        """
        gen = self.get_generation() if generation is None else generation
        if gen != self.get_generation():
            logger.info("request_create dropped: stale gen=%s", gen)
            return False

        mods = modalities or ["text", "audio"]
        pending = _PendingCreate(generation=gen, modalities=list(mods), extra=dict(response_extra))

        async with self._lock:
            if self.is_busy() or force_queue:
                # 覆盖同代 pending；更旧代直接丢
                if self._pending is None or self._pending.generation <= gen:
                    self._pending = pending
                logger.debug(
                    "response.create queued gen=%s busy=%s cancel=%s create=%s",
                    gen,
                    self.is_response_active(),
                    self._cancel_inflight,
                    self._create_inflight,
                )
                return True
            return await self._fire_unlocked(pending)

    async def flush_if_idle(self) -> bool:
        """response.done / cancel 完成后调用：有 pending 则发送。"""
        async with self._lock:
            if self.is_busy():
                return False
            pending = self._pending
            if pending is None:
                return False
            if pending.generation != self.get_generation():
                logger.info(
                    "flush drop stale pending gen=%s current=%s",
                    pending.generation,
                    self.get_generation(),
                )
                self._pending = None
                return False
            self._pending = None
            return await self._fire_unlocked(pending)

    async def cancel_active(self, *, response_id: str | None = None) -> bool:
        """
        取消当前上游 response。无 active 不发，避免协议噪声。
        同时清空 pending（打断语义：旧意图作废；新意图应重新 schedule）。

        注意：成功发出 cancel 后保持 busy（_cancel_inflight），
        直到 on_response_finished / response.done，禁止立刻 create。
        """
        async with self._lock:
            self._pending = None
            if self._cancel_inflight:
                return False
            # 本地认为无 active 且无 create 在途：不发 cancel
            if not self.is_response_active() and not self._create_inflight:
                return False
            self._cancel_inflight = True
            rid = response_id or self._last_response_id
            evt: dict[str, Any] = {"type": "response.cancel"}
            if rid:
                evt["response_id"] = rid
            try:
                await self.send(evt)
                return True
            except Exception:  # noqa: BLE001
                logger.exception("response.cancel failed")
                self._cancel_inflight = False
                return False

    def on_interrupted(self) -> None:
        """用户打断：丢弃排队中的 create。"""
        self._pending = None

    def on_response_created(self, response_id: str | None) -> None:
        self._last_response_id = response_id
        self._create_inflight = False
        # created 说明上一轮 cancel 已结束或本就未 cancel
        self._cancel_inflight = False

    def on_response_finished(self) -> None:
        self._last_response_id = None
        self._cancel_inflight = False
        self._create_inflight = False

    def on_create_rejected(self) -> None:
        """上游拒绝 create（仍有 response in progress）：清 create_inflight，保持可重试。"""
        self._create_inflight = False

    async def after_tool_ready(
        self,
        *,
        generation: int,
        cancel_filler: bool = False,
        is_filler_playing: Callable[[], bool] | None = None,
    ) -> bool:
        """
        工具结果已写入 conversation 后：取消垫词（如有）并调度口语复述。
        """
        if generation != self.get_generation():
            logger.info("after_tool_ready stale gen=%s skip create", generation)
            return False

        filler_on = bool(is_filler_playing and is_filler_playing())
        if cancel_filler and filler_on and self.is_busy():
            await self.cancel_active()
            settle = max(0, self.settle_ms) / 1000.0
            if settle:
                await asyncio.sleep(settle)

        if generation != self.get_generation():
            return False

        return await self.request_create(generation=generation)

    async def _fire_unlocked(self, pending: _PendingCreate) -> bool:
        if pending.generation != self.get_generation():
            return False
        if self.is_busy():
            self._pending = pending
            return True
        body: dict[str, Any] = {
            "type": "response.create",
            "response": {
                "modalities": pending.modalities,
                **pending.extra,
            },
        }
        try:
            self._create_inflight = True
            await self.send(body)
            logger.debug("response.create sent gen=%s", pending.generation)
            return True
        except Exception:  # noqa: BLE001
            self._create_inflight = False
            logger.exception("response.create failed")
            return False
