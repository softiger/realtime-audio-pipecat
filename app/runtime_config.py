"""运行时配置覆盖：.env 为底座，内存覆盖热更；重启丢失。"""

from __future__ import annotations

import threading
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_settings

FieldType = Literal["string", "int", "float", "bool", "enum", "secret", "textarea"]

# 密钥：GET 不回明文；PUT 空串=不改
_SECRET_KEYS = frozenset(
    {
        "dashscope_api_key",
        "dz_private_key",
    }
)

# 进程已绑定，仅展示不可热更
_PROCESS_KEYS = frozenset(
    {
        "host",
        "port",
        "ssl_certfile",
        "ssl_keyfile",
    }
)

# 分类：id → 展示名
CATEGORIES: list[dict[str, str]] = [
    {"id": "dashscope", "label": "百炼 / Realtime"},
    {"id": "turn", "label": "轮次 / VAD / 声纹"},
    {"id": "duplex_speak", "label": "双工 / 播报"},
    {"id": "tool_filler", "label": "工具垫词"},
    {"id": "tool_timeout", "label": "超时 / 兜底"},
    {"id": "dezhu", "label": "得助 OpenAPI"},
    {"id": "server", "label": "服务（只读）"},
]

# 字段元数据：驱动配置页与校验
FIELD_SPECS: list[dict[str, Any]] = [
    # —— 百炼 ——
    {
        "key": "dashscope_api_key",
        "category": "dashscope",
        "label": "DashScope API Key",
        "type": "secret",
        "help": "阿里云百炼密钥；留空保存=不修改",
    },
    {
        "key": "dashscope_workspace_id",
        "category": "dashscope",
        "label": "业务空间 ID",
        "type": "string",
        "help": "填写后走专属 MaaS 域名；可空",
    },
    {
        "key": "realtime_model",
        "category": "dashscope",
        "label": "实时模型",
        "type": "string",
        "help": "如 qwen-audio-3.1-realtime-plus",
    },
    {
        "key": "realtime_voice",
        "category": "dashscope",
        "label": "音色",
        "type": "string",
        "help": "如 longanqian_v3.1",
    },
    # —— 轮次 ——
    {
        "key": "turn_detection",
        "category": "turn",
        "label": "轮次检测",
        "type": "enum",
        "options": ["smart_turn_v2", "smart_turn", "server_vad", "manual"],
        "help": "smart_turn_v2 语义打断更稳；server_vad 更省",
    },
    {
        "key": "server_vad_threshold",
        "category": "turn",
        "label": "VAD 阈值",
        "type": "float",
        "help": "仅 server_vad；0~1",
    },
    {
        "key": "server_vad_silence_duration_ms",
        "category": "turn",
        "label": "静音时长 (ms)",
        "type": "int",
        "help": "仅 server_vad；说完→开想空档",
    },
    {
        "key": "voiceprint_audio_urls",
        "category": "turn",
        "label": "声纹预录 URL",
        "type": "textarea",
        "help": "仅 smart_turn(_v2)；逗号分隔，最多 5；公网 16kHz PCM/WAV",
    },
    # —— 双工 / 播报 ——
    {
        "key": "voice_duplex",
        "category": "duplex_speak",
        "label": "双工策略",
        "type": "enum",
        "options": ["soft", "full", "gate", "half"],
        "help": "soft=推荐；full=随时打断；gate=播报忽略 VAD；half=播报静音上行",
    },
    {
        "key": "speak_mode",
        "category": "duplex_speak",
        "label": "播报文案",
        "type": "enum",
        "options": ["polish", "verbatim"],
        "help": "polish=轻度口语化；verbatim=完整照读",
    },
    {
        "key": "speak_prefix_polish",
        "category": "duplex_speak",
        "label": "polish 朗读前缀",
        "type": "textarea",
        "help": "空=内置模板；开场/垫词 instructions",
    },
    {
        "key": "speak_prefix_verbatim",
        "category": "duplex_speak",
        "label": "verbatim 朗读前缀",
        "type": "textarea",
        "help": "空=内置模板",
    },
    # —— 垫词 ——
    {
        "key": "tool_filler_enabled",
        "category": "tool_filler",
        "label": "启用垫词",
        "type": "bool",
        "help": "得助查询等待超时先播提示",
    },
    {
        "key": "tool_filler_delay_ms",
        "category": "tool_filler",
        "label": "首触延迟 (ms)",
        "type": "int",
        "help": "工具开始后多久仍在跑才播第一句",
    },
    {
        "key": "tool_filler_interval_ms",
        "category": "tool_filler",
        "label": "句间隔 (ms)",
        "type": "int",
        "help": "≤0 只播首触一句",
    },
    {
        "key": "tool_filler_max_count",
        "category": "tool_filler",
        "label": "最多句数",
        "type": "int",
        "help": "单次工具调用；含首触；≤0 按 1",
    },
    {
        "key": "tool_filler_text",
        "category": "tool_filler",
        "label": "垫词正文",
        "type": "string",
        "help": "如「稍等。」",
    },
    {
        "key": "tool_filler_prompt",
        "category": "tool_filler",
        "label": "垫词 user 提示",
        "type": "string",
        "help": "交给模型的 conversation 提示",
    },
    {
        "key": "tool_transcript_wait_ms",
        "category": "tool_filler",
        "label": "转写校准等待 (ms)",
        "type": "int",
        "help": "调工具前等本轮 ASR 校准 question",
    },
    # —— 超时 ——
    {
        "key": "tool_timeout_ms",
        "category": "tool_timeout",
        "label": "业务超时 (ms)",
        "type": "int",
        "help": "得助抖动兜底",
    },
    {
        "key": "tool_fallback_text",
        "category": "tool_timeout",
        "label": "超时兜底话术",
        "type": "textarea",
        "help": "超时后播报",
    },
    {
        "key": "response_settle_ms",
        "category": "tool_timeout",
        "label": "response 沉降 (ms)",
        "type": "int",
        "help": "response.create 紧贴 cancel 时的沉降",
    },
    # —— 得助 ——
    {
        "key": "dz_api_base_url",
        "category": "dezhu",
        "label": "API Base URL",
        "type": "string",
        "help": "如 https://dzai.dezhuyun.com",
    },
    {
        "key": "dz_app_id",
        "category": "dezhu",
        "label": "App ID",
        "type": "string",
        "help": "得助应用 ID",
    },
    {
        "key": "dz_channel_id",
        "category": "dezhu",
        "label": "Channel ID",
        "type": "string",
        "help": "渠道 ID",
    },
    {
        "key": "dz_private_key",
        "category": "dezhu",
        "label": "私钥",
        "type": "secret",
        "help": "PKCS#8；留空保存=不修改",
    },
    {
        "key": "dz_sign_mode",
        "category": "dezhu",
        "label": "签名模式",
        "type": "enum",
        "options": ["sha256_pkcs1", "sha1_pkcs1", "pss_sha256"],
        "help": "默认 sha256_pkcs1",
    },
    {
        "key": "dz_reply_path",
        "category": "dezhu",
        "label": "Reply 路径",
        "type": "string",
        "help": "默认 /chat/openapi/v1/chat/reply",
    },
    # —— 服务只读 ——
    {
        "key": "host",
        "category": "server",
        "label": "监听 Host",
        "type": "string",
        "help": "进程已绑定，改 .env 后重启",
        "readonly": True,
    },
    {
        "key": "port",
        "category": "server",
        "label": "端口",
        "type": "int",
        "help": "进程已绑定，改 .env 后重启",
        "readonly": True,
    },
    {
        "key": "ssl_certfile",
        "category": "server",
        "label": "SSL 证书路径",
        "type": "string",
        "help": "进程级；空=不启用应用内 TLS",
        "readonly": True,
    },
    {
        "key": "ssl_keyfile",
        "category": "server",
        "label": "SSL 私钥路径",
        "type": "string",
        "help": "进程级",
        "readonly": True,
    },
]

_SPEC_BY_KEY: dict[str, dict[str, Any]] = {s["key"]: s for s in FIELD_SPECS}
_RUNTIME_KEYS = frozenset(
    k for k, s in _SPEC_BY_KEY.items() if not s.get("readonly") and k not in _PROCESS_KEYS
)

_lock = threading.RLock()
_overrides: dict[str, Any] = {}


class ConfigPutBody(BaseModel):
    """部分更新；未出现的键保持原覆盖/底座。"""

    values: dict[str, Any] = Field(default_factory=dict)
    # true：先清空全部覆盖再应用 values
    replace: bool = False


def get_overrides() -> dict[str, Any]:
    with _lock:
        return dict(_overrides)


def clear_overrides() -> None:
    with _lock:
        _overrides.clear()


def get_effective_settings() -> Settings:
    """底座 Settings + 运行时覆盖（model_copy，不改 lru 缓存实例）。"""
    base = get_settings()
    with _lock:
        if not _overrides:
            return base
        return base.model_copy(update=dict(_overrides))


def _coerce_value(spec: dict[str, Any], raw: Any) -> Any:
    t = spec.get("type") or "string"
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"布尔值无效: {raw!r}")
    if t == "int":
        return int(raw)
    if t == "float":
        return float(raw)
    if t == "enum":
        s = str(raw).strip()
        opts = spec.get("options") or []
        if opts and s not in opts:
            raise ValueError(f"可选值: {', '.join(opts)}")
        return s
    # string / secret / textarea
    if raw is None:
        return ""
    return str(raw)


def apply_overrides(values: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    """校验并写入覆盖；返回新的 overrides 快照。

    密钥字段空串 → 跳过（保留当前生效值，不写入覆盖清空）。
    readonly / 进程字段 → 拒绝。
    """
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    for key, raw in (values or {}).items():
        spec = _SPEC_BY_KEY.get(key)
        if spec is None:
            errors.append(f"未知字段: {key}")
            continue
        if spec.get("readonly") or key in _PROCESS_KEYS:
            errors.append(f"只读/进程级不可热更: {key}")
            continue
        if key not in _RUNTIME_KEYS:
            errors.append(f"不可热更: {key}")
            continue
        if key in _SECRET_KEYS:
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue  # 空=不改
        try:
            cleaned[key] = _coerce_value(spec, raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"{key}: {exc}")

    if errors:
        raise ValueError("; ".join(errors))

    # 用 Settings 再验一遍类型边界
    try:
        probe = get_settings().model_copy(update={**get_overrides(), **cleaned})
        # 触发一次属性访问
        _ = probe.has_api_key, probe.has_dezhu
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    with _lock:
        if replace:
            _overrides.clear()
        _overrides.update(cleaned)
        return dict(_overrides)


def _public_value(key: str, effective: Settings) -> Any:
    """对外展示值：密钥不回明文。"""
    raw = getattr(effective, key, None)
    if key in _SECRET_KEYS:
        return ""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return ""
    return raw


def _secret_configured(key: str, effective: Settings) -> bool:
    v = getattr(effective, key, None)
    if not isinstance(v, str):
        return bool(v)
    s = v.strip()
    if key == "dashscope_api_key":
        return bool(s) and s not in {"sk-xxxxxxxx", "YOUR_API_KEY", "xxx"}
    return bool(s)


def build_config_snapshot() -> dict[str, Any]:
    """GET /api/config 载荷：分类 + 字段 + 当前生效值 + 覆盖标记。"""
    effective = get_effective_settings()
    base = get_settings()
    with _lock:
        ov = dict(_overrides)

    fields_out: list[dict[str, Any]] = []
    for spec in FIELD_SPECS:
        key = spec["key"]
        item: dict[str, Any] = {
            "key": key,
            "category": spec["category"],
            "label": spec["label"],
            "type": spec["type"],
            "help": spec.get("help") or "",
            "readonly": bool(spec.get("readonly") or key in _PROCESS_KEYS),
            "options": spec.get("options"),
            "value": _public_value(key, effective),
            "overridden": key in ov,
            "base_value": _public_value(key, base) if key not in _SECRET_KEYS else "",
        }
        if key in _SECRET_KEYS:
            item["configured"] = _secret_configured(key, effective)
            item["base_configured"] = _secret_configured(key, base)
        fields_out.append(item)

    return {
        "ok": True,
        "runtime_only": True,
        "note": "仅内存热更，重启恢复 .env；下一通 Session / 下一次文本请求生效",
        "categories": CATEGORIES,
        "fields": fields_out,
        "override_keys": sorted(ov.keys()),
        "flags": {
            "has_api_key": effective.has_api_key,
            "has_dezhu": effective.has_dezhu,
            "has_workspace": bool((effective.dashscope_workspace_id or "").strip()),
        },
    }
