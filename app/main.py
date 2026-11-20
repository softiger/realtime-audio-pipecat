"""FastAPI 入口：科幻语音页 + 得助大脑桥接。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings
from app.dezhu.client import DezhuChatClient, extract_speakable_text
from app.dezhu.session import CallSession
from app.pipeline import run_voice_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dezhu-voice")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    dezhu = DezhuChatClient(settings)
    app.state.dezhu = dezhu
    # 文本 demo：call_id → CallSession（多轮复用得助 sessionId）
    app.state.text_sessions = {}
    logger.info(
        "DEZHU-Voice ready | engine=pipecat | model=%s | voice=%s | turn=%s | dz=%s",
        settings.realtime_model,
        settings.realtime_voice,
        settings.turn_detection,
        settings.has_dezhu,
    )
    if not settings.has_api_key:
        logger.warning("DASHSCOPE_API_KEY not set")
    if not settings.has_dezhu:
        logger.warning("得助凭证未配齐，语音业务脑将不可用")
    yield
    await dezhu.aclose()


app = FastAPI(
    title="得助全双工实时语音机器人 (Pipecat)",
    version="0.2.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class TextChatRequest(BaseModel):
    """文本 demo 提问。"""

    ask: str = Field(default="", description="用户问题；welcome=true 时可空")
    call_id: str | None = Field(default=None, description="本系统会话 ID，空则新开")
    welcome: bool = Field(default=False, description="是否走 WELCOME 开场")


def _resolve_text_session(store: dict[str, CallSession], call_id: str | None) -> CallSession:
    if call_id and call_id in store:
        return store[call_id]
    call = CallSession()
    call.terminal_id = f"text-{call.call_id[:16]}"
    store[call.call_id] = call
    return call


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/text")
async def text_demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "text.html")


@app.get("/api/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "engine": "pipecat",
        "model": settings.realtime_model,
        "voice": settings.realtime_voice,
        "turn_detection": settings.turn_detection,
        "voice_duplex": settings.voice_duplex,
        "tool_timeout_ms": settings.tool_timeout_ms,
        "has_api_key": settings.has_api_key,
        "has_workspace": bool(settings.dashscope_workspace_id),
        "has_dezhu": settings.has_dezhu,
        "dz_base": settings.dz_api_base_url,
        "dz_channel": ((settings.dz_channel_id or "")[:8] + "...")
        if settings.dz_channel_id
        else "",
    }


@app.post("/api/chat")
async def text_chat(body: TextChatRequest) -> dict[str, Any]:
    """文本框问答 → 得助 reply → 纯文本答案（可多轮）。"""
    settings = get_settings()
    if not settings.has_dezhu:
        raise HTTPException(status_code=503, detail="得助凭证未配置")

    ask = (body.ask or "").strip()
    if not ask and not body.welcome:
        raise HTTPException(status_code=400, detail="ask 不能为空")

    store: dict[str, CallSession] = app.state.text_sessions
    call = _resolve_text_session(store, body.call_id)
    dezhu: DezhuChatClient = app.state.dezhu

    try:
        if body.welcome:
            raw = await dezhu.welcome(call)
            text = extract_speakable_text(raw) or raw.get("message") or "您好，有什么可以帮您？"
        else:
            text, raw = await dezhu.chat(ask, call)
    except Exception as exc:  # noqa: BLE001
        logger.exception("text chat failed call_id=%s", call.call_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "ok": True,
        "call_id": call.call_id,
        "dezhu_session": call.dezhu_session,
        "turn_count": call.turn_count,
        "text": text,
        "raw": raw,
    }


@app.websocket("/ws/voice")
async def voice_ws(websocket: WebSocket) -> None:
    """浏览器全双工语音：Pipecat Pipeline + Qwen Realtime + 得助 FC。"""
    await websocket.accept()
    settings = get_settings()
    dezhu: DezhuChatClient = websocket.app.state.dezhu
    # 每次通话新建 session（得助 sessionId 在通话内多轮复用）
    call = CallSession()
    try:
        await run_voice_bot(websocket, settings=settings, dezhu=dezhu, call=call)
    except WebSocketDisconnect:
        logger.info("browser disconnected call_id=%s", call.call_id)
    except Exception:  # noqa: BLE001
        logger.exception("voice ws error call_id=%s", call.call_id)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    import uvicorn

    settings = get_settings()
    ssl_cert = (settings.ssl_certfile or "").strip()
    ssl_key = (settings.ssl_keyfile or "").strip()
    run_kw: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "reload": False,
        "log_level": "info",
    }
    # 单容器 HTTPS：SSL_CERTFILE + SSL_KEYFILE（compose 默认走 Nginx 终结 TLS）
    if ssl_cert and ssl_key:
        run_kw["ssl_certfile"] = ssl_cert
        run_kw["ssl_keyfile"] = ssl_key
        logger.info("TLS on | cert=%s", ssl_cert)
    uvicorn.run("app.main:app", **run_kw)


if __name__ == "__main__":
    main()
