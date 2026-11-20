"""得助文本应答 HTTP 客户端。"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import httpx

from app.config import Settings
from app.dezhu.session import CallSession
from app.dezhu.sign import flatten_json_for_sign, rsa_sign

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    plain = _TAG_RE.sub("", text)
    plain = (
        plain.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return _WS_RE.sub(" ", plain).strip()


def extract_speakable_text(payload: dict[str, Any]) -> str:
    """
    从得助 reply 响应抽出适合语音播报的纯文本。

    优先顺序：
      normalAnswer → welcome/channelWelcome → listAnswer 拼接 → multiList
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""

    answer_vo = data.get("answerVo") or {}
    if not isinstance(answer_vo, dict):
        answer_vo = {}

    chunks: list[str] = []

    normal = answer_vo.get("normalAnswer")
    if normal:
        chunks.append(_strip_html(str(normal)))

    welcome_vo = answer_vo.get("welcomeAnswerVO") or {}
    if isinstance(welcome_vo, dict):
        channel = welcome_vo.get("channelWelcome") or {}
        if isinstance(channel, dict) and channel.get("normalAnswer"):
            chunks.append(_strip_html(str(channel["normalAnswer"])))
        # 若主答案空，尝试 welcome 里的 normal
        if not chunks and welcome_vo.get("normalAnswer"):
            chunks.append(_strip_html(str(welcome_vo["normalAnswer"])))

        recommend = welcome_vo.get("recommend") or {}
        if isinstance(recommend, dict):
            guide = recommend.get("guideWords")
            cards = recommend.get("guideCards") or []
            rec_lines: list[str] = []
            if isinstance(cards, list):
                for card in cards[:2]:
                    if not isinstance(card, dict):
                        continue
                    for item in (card.get("recommends") or [])[:5]:
                        if isinstance(item, dict) and item.get("standardQuestion"):
                            rec_lines.append(str(item["standardQuestion"]))
            if rec_lines:
                prefix = _strip_html(str(guide)) if guide else "您可以这样问我"
                chunks.append(prefix + "：" + "；".join(rec_lines))

    guide_words = answer_vo.get("guideWords")
    list_answer = answer_vo.get("listAnswer") or []
    if isinstance(list_answer, list) and list_answer:
        items = []
        for row in list_answer[:8]:
            if isinstance(row, dict) and row.get("answer"):
                items.append(_strip_html(str(row["answer"])))
            elif isinstance(row, str):
                items.append(_strip_html(row))
        if items:
            prefix = _strip_html(str(guide_words)) if guide_words else "相关问题"
            # 已有正文时列表作补充
            if chunks:
                chunks.append(prefix + "：" + "；".join(items))
            else:
                chunks.append(prefix + "：" + "；".join(items))

    multi = answer_vo.get("multiList") or answer_vo.get("multiListAnswer") or []
    if isinstance(multi, list):
        for block in multi[:3]:
            if not isinstance(block, dict):
                continue
            if block.get("normalAnswer"):
                chunks.append(_strip_html(str(block["normalAnswer"])))
            sub = block.get("listAnswer") or []
            if isinstance(sub, list) and sub:
                labels = [
                    _strip_html(str(x.get("answer")))
                    for x in sub[:6]
                    if isinstance(x, dict) and x.get("answer")
                ]
                if labels:
                    chunks.append("可选：" + "；".join(labels))

    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for c in chunks:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    return "。".join(ordered) if ordered else ""


class DezhuChatClient:
    """
    POST /chat/openapi/v1/chat/reply

    Header: appId / timestamp / sign
    Body: version=2 + channelId + ask + askType + sessionId + logId + terminalId
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    def _auth_headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        app_id = self.settings.dz_app_id.strip()
        ts = str(int(__import__("time").time() * 1000))
        mode = (self.settings.dz_sign_mode or "sha1_pkcs1").strip().lower()

        if mode in {"pss_sha256", "sha256_pss", "pss"}:
            # 新鉴权：appId + timestamp + params + body扁平
            body_flat = flatten_json_for_sign(body or {})
            content = f"{app_id}{ts}{body_flat}"
        else:
            content = f"{app_id}{ts}"

        sign = rsa_sign(content, self.settings.dz_private_key, mode=mode)
        return {
            "appId": app_id,
            "timestamp": ts,
            "sign": sign,
            "Content-Type": "application/json",
        }

    async def reply(
        self,
        *,
        ask: str,
        call: CallSession,
        ask_type: str = "USER_INPUT",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "version": "2",
            "terminalId": call.terminal_id,
            "channelId": self.settings.dz_channel_id.strip(),
            "ask": ask,
            "askType": ask_type,
            "logId": str(uuid.uuid4()),
        }
        if call.external_id:
            body["externalId"] = call.external_id
        if call.dezhu_session:
            body["sessionId"] = call.dezhu_session

        headers = self._auth_headers(body)
        url = self.settings.dz_reply_url()
        logger.info(
            "dezhu reply ask_type=%s session=%s ask=%s",
            ask_type,
            call.dezhu_session or "-",
            ask[:80],
        )

        resp = await self._client.post(url, headers=headers, json=body)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            text = resp.text[:500]
            raise RuntimeError(f"得助接口非 JSON 响应 HTTP {resp.status_code}: {text}") from None

        if resp.status_code >= 400:
            raise RuntimeError(f"得助 HTTP {resp.status_code}: {data}")

        code = str(data.get("code", ""))
        if code and code != "10000":
            raise RuntimeError(f"得助业务错误 code={code} message={data.get('message')}")

        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        sid = nested.get("sessionId")
        if sid:
            call.bind_dezhu_session(str(sid))
        call.touch_turn()
        return data

    async def welcome(self, call: CallSession) -> dict[str, Any]:
        return await self.reply(ask="welcome", call=call, ask_type="WELCOME")

    async def chat(self, question: str, call: CallSession) -> tuple[str, dict[str, Any]]:
        """返回 (可播报文本, 原始响应)。"""
        raw = await self.reply(ask=question, call=call, ask_type="USER_INPUT")
        text = extract_speakable_text(raw)
        if not text:
            text = raw.get("message") or "抱歉，我暂时没有找到合适的答案。"
        return text, raw
