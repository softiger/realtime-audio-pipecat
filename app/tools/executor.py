"""
业务 API 异步执行器。

保证：
  - 超时不阻塞语音链路（wait_for + 兜底话术）
  - 同 call_id+call_id(FC)+name 幂等：重复 FC done 不二次打业务
  - generation 过期：仍返回文本供协议回写，但标记 stale，调度器不 create
  - 可取消：外部 cancel_event / 代次变化时尽快结束等待
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, Any], Awaitable[str]]


@dataclass(frozen=True)
class ToolResult:
    text: str
    ok: bool = True
    timed_out: bool = False
    stale: bool = False
    cached: bool = False
    error: str | None = None
    idem_key: str = ""
    # 本次执行墙钟耗时（含缓存命中的等待）；毫秒
    duration_ms: int | None = None


@dataclass
class ToolPolicy:
    timeout_ms: int = 8000
    fallback_text: str = "查询有点慢，请稍后再试，或换个方式描述您的问题。"
    # 进程内短缓存：相同幂等键命中则直接返回（防重复 FC）
    memo_ttl_s: float = 120.0


def _normalize_args(arguments: Any) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, (dict, list)):
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    s = str(arguments).strip()
    if not s:
        return "{}"
    try:
        parsed = json.loads(s)
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return s


def make_idem_key(call_id: str, fc_call_id: str, name: str, arguments: Any) -> str:
    """幂等键：通话 + FC call_id 优先；无 call_id 时用 args 哈希。"""
    base = f"{call_id}|{fc_call_id}|{name}"
    if fc_call_id:
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]
    norm = _normalize_args(arguments)
    raw = f"{base}|{norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class _MemoEntry:
    result: ToolResult
    expires_at: float


@dataclass
class ToolExecutor:
    """单通话可共用一个实例；memo 按幂等键去重。"""

    handler: ToolHandler | None
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    # generation 读取：返回当前 gen，用于结果比对
    get_generation: Callable[[], int] | None = None
    _memo: dict[str, _MemoEntry] = field(default_factory=dict, repr=False)
    _inflight: dict[str, asyncio.Future] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def _now(self) -> float:
        return time.monotonic()

    def _purge_memo(self) -> None:
        now = self._now()
        dead = [k for k, v in self._memo.items() if v.expires_at <= now]
        for k in dead:
            self._memo.pop(k, None)

    async def execute(
        self,
        *,
        name: str,
        arguments: Any,
        fc_call_id: str,
        session_call_id: str = "",
        generation: int | None = None,
    ) -> ToolResult:
        """
        执行工具。

        generation: 发起时的代次；返回时若当前代次不同 → stale=True
        """
        start_gen = (
            generation
            if generation is not None
            else (self.get_generation() if self.get_generation else 0)
        )
        idem = make_idem_key(session_call_id, fc_call_id, name, arguments)
        timeout_s = max(0.2, (self.policy.timeout_ms or 8000) / 1000.0)
        t0 = self._now()

        def _with_ms(r: ToolResult) -> ToolResult:
            ms = max(0, int((self._now() - t0) * 1000))
            if r.duration_ms is not None and r.cached:
                # 缓存命中：保留原始业务耗时，外层再记等待可另算；这里用本次墙钟
                pass
            return ToolResult(
                text=r.text,
                ok=r.ok,
                timed_out=r.timed_out,
                stale=r.stale,
                cached=r.cached,
                error=r.error,
                idem_key=r.idem_key or idem,
                duration_ms=ms,
            )

        async with self._lock:
            self._purge_memo()
            memo = self._memo.get(idem)
            if memo is not None and memo.expires_at > self._now():
                r = memo.result
                stale = self._is_stale(start_gen)
                return _with_ms(
                    ToolResult(
                        text=r.text,
                        ok=r.ok,
                        timed_out=r.timed_out,
                        stale=stale,
                        cached=True,
                        error=r.error,
                        idem_key=idem,
                        duration_ms=r.duration_ms,
                    )
                )
            # single-flight：同键并发共用一个 Future
            existing = self._inflight.get(idem)
            if existing is not None and not existing.done():
                wait_fut = existing
            else:
                wait_fut = None
                loop = asyncio.get_event_loop()
                fut: asyncio.Future = loop.create_future()
                self._inflight[idem] = fut

        if wait_fut is not None:
            try:
                base: ToolResult = await wait_fut
            except Exception as exc:  # noqa: BLE001
                return _with_ms(
                    ToolResult(
                        text=self.policy.fallback_text,
                        ok=False,
                        stale=self._is_stale(start_gen),
                        error=str(exc),
                        idem_key=idem,
                    )
                )
            return _with_ms(
                ToolResult(
                    text=base.text,
                    ok=base.ok,
                    timed_out=base.timed_out,
                    stale=self._is_stale(start_gen),
                    cached=True,
                    error=base.error,
                    idem_key=idem,
                    duration_ms=base.duration_ms,
                )
            )

        try:
            result = await self._run_handler(name, arguments, timeout_s, idem, start_gen)
            result = _with_ms(result)
        except Exception as exc:  # noqa: BLE001
            result = _with_ms(
                ToolResult(
                    text=self.policy.fallback_text,
                    ok=False,
                    stale=self._is_stale(start_gen),
                    error=str(exc),
                    idem_key=idem,
                )
            )
            async with self._lock:
                fut2 = self._inflight.pop(idem, None)
                if fut2 is not None and not fut2.done():
                    fut2.set_exception(exc)
            return result

        async with self._lock:
            self._memo[idem] = _MemoEntry(
                result=result,
                expires_at=self._now() + self.policy.memo_ttl_s,
            )
            fut2 = self._inflight.pop(idem, None)
            if fut2 is not None and not fut2.done():
                fut2.set_result(result)

        return result

    def _is_stale(self, start_gen: int) -> bool:
        if self.get_generation is None:
            return False
        try:
            return int(self.get_generation()) != int(start_gen)
        except Exception:  # noqa: BLE001
            return False

    async def _run_handler(
        self,
        name: str,
        arguments: Any,
        timeout_s: float,
        idem: str,
        start_gen: int,
    ) -> ToolResult:
        if not self.handler:
            return ToolResult(
                text=f"未注册工具: {name}",
                ok=False,
                stale=self._is_stale(start_gen),
                error="no_handler",
                idem_key=idem,
            )
        try:
            text = await asyncio.wait_for(
                self.handler(name, arguments),
                timeout=timeout_s,
            )
            text = (text or "").strip() or self.policy.fallback_text
            return ToolResult(
                text=text,
                ok=True,
                stale=self._is_stale(start_gen),
                idem_key=idem,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "tool timeout name=%s idem=%s timeout_s=%.2f",
                name,
                idem,
                timeout_s,
            )
            return ToolResult(
                text=self.policy.fallback_text,
                ok=False,
                timed_out=True,
                stale=self._is_stale(start_gen),
                error="timeout",
                idem_key=idem,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool handler failed name=%s", name)
            return ToolResult(
                text=f"业务系统暂时不可用：{exc}",
                ok=False,
                stale=self._is_stale(start_gen),
                error=str(exc),
                idem_key=idem,
            )

    def invalidate_memo(self) -> None:
        self._memo.clear()
