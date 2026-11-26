"""应用配置：环境变量 → 强类型 Settings。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashscope_api_key: str = ""
    dashscope_workspace_id: str = ""

    realtime_model: str = "qwen-audio-3.1-realtime-plus"
    realtime_voice: str = "longanqian_v3.1"
    turn_detection: str = "smart_turn_v2"
    # 仅 TURN_DETECTION=server_vad 时写入 turn_detection
    server_vad_threshold: float = 0.5
    server_vad_silence_duration_ms: int = 500
    # 声纹预录公网 URL，逗号/分号/换行分隔；仅 smart_turn(_v2) 生效，最多 5 条
    voiceprint_audio_urls: str = ""

    # 全双工策略：full=随时可打断 | gate=播报中忽略 VAD 打断 | half=播报中上行静音
    # soft=服务端可打断，前端播报中低能量不上送（外放更稳）
    voice_duplex: str = "soft"

    # 播报文案策略：polish=忠实内容可轻度口语化 | verbatim=完整照读不增删改写
    # 同时约束：会话 instructions（工具结果复述）+ speak_text（开场/垫词）
    speak_mode: str = "polish"

    # 得助查询等待：超时未返回则先播垫词（上游 TTS）
    tool_filler_enabled: bool = True
    # 首触：工具开始后多久仍在跑才播第一句
    tool_filler_delay_ms: int = 450
    # 多次：两句垫词之间的间隔；<=0 则只播首触一句
    tool_filler_interval_ms: int = 3000
    # 单次工具调用最多垫词句数（含首触）；<=0 按 1
    tool_filler_max_count: int = 1
    tool_filler_text: str = "稍等。"
    # speak_text 的 conversation user 提示（垫词专用）
    tool_filler_prompt: str = "（系统）请播报等待提示。"

    # 开场/垫词朗读 instructions 前缀；空则用 session_config 内置模板
    speak_prefix_polish: str = ""
    speak_prefix_verbatim: str = ""

    # 工具入参：等本轮 ASR 完成再校准 question（毫秒）
    tool_transcript_wait_ms: int = 550

    # 业务 API：超时兜底，避免得助抖动拖死整通电话
    tool_timeout_ms: int = 8000
    tool_fallback_text: str = "查询有点慢，请稍后再试，或换个方式描述您的问题。"
    # response.create 紧贴 cancel 时的沉降（毫秒）
    response_settle_ms: int = 50

    # 得助 OpenAPI
    dz_api_base_url: str = "https://dzai.dezhuyun.com"
    dz_app_id: str = ""
    dz_channel_id: str = ""
    dz_private_key: str = ""
    dz_sign_mode: str = "sha256_pkcs1"  # sha1_pkcs1 | sha256_pkcs1 | pss_sha256
    dz_reply_path: str = "/chat/openapi/v1/chat/reply"

    host: str = "0.0.0.0"
    port: int = 8766
    # 容器内直挂 TLS（无 Nginx 时）：挂载证书并设路径
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @property
    def has_api_key(self) -> bool:
        key = (self.dashscope_api_key or "").strip()
        return bool(key) and key not in {"sk-xxxxxxxx", "YOUR_API_KEY", "xxx"}

    @property
    def has_dezhu(self) -> bool:
        return bool(
            (self.dz_app_id or "").strip()
            and (self.dz_channel_id or "").strip()
            and (self.dz_private_key or "").strip()
            and (self.dz_api_base_url or "").strip()
        )

    def parsed_voiceprint_audio_urls(self) -> list[str]:
        """解析声纹 URL；空项丢弃，去重保序，上限 5（百炼协议）。"""
        raw = (self.voiceprint_audio_urls or "").strip()
        if not raw:
            return []
        parts: list[str] = []
        for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
            u = chunk.strip()
            if u:
                parts.append(u)
        seen: set[str] = set()
        out: list[str] = []
        for u in parts:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
            if len(out) >= 5:
                break
        return out

    def realtime_ws_url(self) -> str:
        model = self.realtime_model
        ws = (self.dashscope_workspace_id or "").strip()
        if ws:
            host = f"{ws}.cn-beijing.maas.aliyuncs.com"
        else:
            host = "dashscope.aliyuncs.com"
        return f"wss://{host}/api-ws/v1/realtime?model={model}"

    def dz_reply_url(self) -> str:
        base = (self.dz_api_base_url or "").rstrip("/")
        path = self.dz_reply_path if self.dz_reply_path.startswith("/") else f"/{self.dz_reply_path}"
        return f"{base}{path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
