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

    # 全双工策略：full=随时可打断 | gate=播报中忽略 VAD 打断 | half=播报中上行静音
    # soft=服务端可打断，前端播报中低能量不上送（外放更稳）
    voice_duplex: str = "soft"

    # 得助查询等待：超时未返回则先播一句垫词（上游 TTS）
    tool_filler_enabled: bool = True
    tool_filler_delay_ms: int = 450
    tool_filler_text: str = "稍等，我帮您查一下。"

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
