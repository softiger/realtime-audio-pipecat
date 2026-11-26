"""Qwen Realtime session.update 载荷（与百炼 OpenAI-Realtime 兼容协议对齐）。"""

from __future__ import annotations

from typing import Any, Literal

from app.config import Settings

SpeakMode = Literal["verbatim", "polish"]

# 语音层只做 ASR/VAD/TTS/FC；业务答案强制走得助
_INSTRUCTIONS_BASE = """你是全双工语音客服的「语音界面」。
业务知识、产品政策、办事流程、活动规则等，一律必须调用工具 query_business_bot 获取标准答复。
闲聊寒暄也可以调用工具；若工具返回欢迎或推荐问题，请按工具内容播报。
除非用户要求，不要输出 emoji、Markdown 或条目符号，尽量纯文本方便播报。
回答尽量简洁完整，先说结论再补关键细节。"""

# 工具结果 / 会话级：润色 vs 照读
_INSTRUCTIONS_RETELL: dict[str, str] = {
    "polish": (
        "拿到工具结果后，用自然口语中文复述，忠实于工具内容，"
        "可轻度口语化，但不要编造业务事实。"
    ),
    "verbatim": (
        "拿到工具结果后，用自然中文完整照读工具正文，"
        "不要增删改写、不要概括省略、不要编造业务事实。"
    ),
}

# 开场白 / 垫词 speak_text：润色 vs 照读
_SPEAK_PREFIX: dict[str, str] = {
    "polish": (
        "请用自然亲切的中文口语播报下面内容，"
        "可轻度口语化，但不要改变原意、不要反问、不要调用任何工具：\n"
    ),
    "verbatim": (
        "请用自然亲切的中文口语完整朗读下面内容，"
        "不要增删改写，不要反问，不要调用任何工具：\n"
    ),
}


def normalize_speak_mode(raw: str | None) -> SpeakMode:
    """env/配置 → 合法 speak_mode；未知值回落 polish。"""
    m = (raw or "polish").strip().lower()
    if m in {"verbatim", "read", "exact", "literal"}:
        return "verbatim"
    if m in {"polish", "paraphrase", "oral", "natural"}:
        return "polish"
    return "polish"


def build_instructions(speak_mode: str | None = None) -> str:
    """会话级 instructions：人格底座 + 复述策略。"""
    mode = normalize_speak_mode(speak_mode)
    return f"{_INSTRUCTIONS_BASE}\n{_INSTRUCTIONS_RETELL[mode]}"


def build_speak_instructions(
    text: str,
    speak_mode: str | None = None,
    *,
    prefix_polish: str | None = None,
    prefix_verbatim: str | None = None,
) -> str:
    """单次 response.create 用的朗读 instructions（开场/垫词）。

    prefix_* 非空时覆盖内置模板（来自 env SPEAK_PREFIX_POLISH / VERBATIM）。
    """
    mode = normalize_speak_mode(speak_mode)
    body = (text or "").strip()
    if mode == "verbatim":
        raw = (prefix_verbatim or "").strip()
    else:
        raw = (prefix_polish or "").strip()
    if raw:
        prefix = raw if raw.endswith("\n") else f"{raw}\n"
    else:
        prefix = _SPEAK_PREFIX[mode]
    return f"{prefix}{body}"


# 兼容旧引用：默认等于 polish 会话词
DEFAULT_INSTRUCTIONS = build_instructions("polish")

QUERY_BUSINESS_BOT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "query_business_bot",
    "description": (
        "查询得助智能文本机器人，获取业务标准答复。"
        "用户提出的咨询、办理、政策、活动、投诉引导等业务问题都必须调用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "用户的完整问题原文或清晰转写",
            }
        },
        "required": ["question"],
    },
}


def build_turn_detection(
    mode: str,
    *,
    voiceprint_audio_urls: list[str] | None = None,
    server_vad_threshold: float = 0.5,
    server_vad_silence_duration_ms: int = 500,
) -> dict[str, Any] | None:
    """轮次检测：smart_turn_v2 语义打断更稳；server_vad 更省；manual 推送说话。

    voiceprint_audio_urls 仅 smart_turn / smart_turn_v2 挂载；须公网可访问的
    16kHz PCM/WAV，且仅首次 session.update 生效（百炼约束）。
    server_vad_* 仅 server_vad 写入 threshold / silence_duration_ms。
    """
    m = (mode or "smart_turn_v2").strip().lower()
    if m in {"manual", "push_to_talk", "push-to-talk"}:
        return None
    if m == "server_vad":
        # 阈值与静音来自 env；静音略短可降低「说完 → 开想」空档
        th = float(server_vad_threshold)
        th = min(1.0, max(0.0, th))
        silence = max(0, int(server_vad_silence_duration_ms))
        return {
            "type": "server_vad",
            "threshold": th,
            "silence_duration_ms": silence,
        }
    # smart_turn / smart_turn_v2（默认 v2）
    td: dict[str, Any] = {
        "type": "smart_turn" if m == "smart_turn" else "smart_turn_v2",
    }
    urls = [u for u in (voiceprint_audio_urls or []) if (u or "").strip()][:5]
    if urls:
        td["voiceprint_audio_urls"] = urls
    return td


def build_session_dict(settings: Settings) -> dict[str, Any]:
    return {
        "modalities": ["text", "audio"],
        "voice": settings.realtime_voice,
        "instructions": build_instructions(settings.speak_mode),
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "input_audio_transcription": {"model": "gummy-realtime-v1"},
        "turn_detection": build_turn_detection(
            settings.turn_detection,
            voiceprint_audio_urls=settings.parsed_voiceprint_audio_urls(),
            server_vad_threshold=settings.server_vad_threshold,
            server_vad_silence_duration_ms=settings.server_vad_silence_duration_ms,
        ),
        "tools": [QUERY_BUSINESS_BOT_TOOL],
        "tool_choice": "auto",
    }


def build_session_update(settings: Settings) -> dict[str, Any]:
    return {"type": "session.update", "session": build_session_dict(settings)}
