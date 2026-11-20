"""Qwen Realtime session.update 载荷（与百炼 OpenAI-Realtime 兼容协议对齐）。"""

from __future__ import annotations

from typing import Any

from app.config import Settings

# 语音层只做 ASR/VAD/TTS/FC；业务答案强制走得助
DEFAULT_INSTRUCTIONS = """你是全双工语音客服的「语音界面」。
业务知识、产品政策、办事流程、活动规则等，一律必须调用工具 query_business_bot 获取标准答复。
拿到工具结果后，用自然口语中文复述，忠实于工具内容，可轻度口语化，但不要编造业务事实。
闲聊寒暄也可以调用工具；若工具返回欢迎或推荐问题，请自然播报。
除非用户要求，不要输出 emoji、Markdown 或条目符号，尽量纯文本方便播报。
回答尽量简洁完整，先说结论再补关键细节。"""

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


def build_turn_detection(mode: str) -> dict[str, Any] | None:
    """轮次检测：smart_turn_v2 语义打断更稳；server_vad 更省；manual 推送说话。"""
    m = (mode or "smart_turn_v2").strip().lower()
    if m in {"manual", "push_to_talk", "push-to-talk"}:
        return None
    if m == "server_vad":
        return {
            "type": "server_vad",
            "threshold": 0.5,
            # 略收紧静音，降低「说完 → 模型开想」的空档
            "silence_duration_ms": 500,
        }
    return {"type": "smart_turn_v2"}


def build_session_dict(settings: Settings) -> dict[str, Any]:
    return {
        "modalities": ["text", "audio"],
        "voice": settings.realtime_voice,
        "instructions": DEFAULT_INSTRUCTIONS,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "input_audio_transcription": {"model": "gummy-realtime-v1"},
        "turn_detection": build_turn_detection(settings.turn_detection),
        "tools": [QUERY_BUSINESS_BOT_TOOL],
        "tool_choice": "auto",
    }


def build_session_update(settings: Settings) -> dict[str, Any]:
    return {"type": "session.update", "session": build_session_dict(settings)}
