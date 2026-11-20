"""工具入参：从 FC arguments 解析 question，并用本轮 ASR 转写校准。"""

from __future__ import annotations

import json
import re
from typing import Any

_FRAG_TAIL = re.compile(r"(嗯+|啊+|呃+|那个|就是|然后|啥|什么)$")


def parse_tool_question(arguments: Any) -> str:
    if isinstance(arguments, str):
        raw = arguments.strip()
        if not raw:
            return ""
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    elif isinstance(arguments, dict):
        parsed = arguments
    else:
        parsed = {}

    if isinstance(parsed, dict):
        return str(parsed.get("question") or parsed.get("ask") or "").strip()
    return str(parsed or "").strip()


def _looks_fragment(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 4:
        return True
    if _FRAG_TAIL.search(s):
        return True
    # 过短且无问号/业务动词，多半是半句
    if len(s) < 8 and "？" not in s and "?" not in s:
        return True
    return False


def calibrate_question(*, model_question: str, transcript: str) -> str:
    """
    业务问句以本轮转写为锚：
      - 无模型问句 → 用转写
      - 模型过短/半句且转写更完整 → 用转写
      - 转写明显更长 → 用转写
      - 否则保留模型（可能已口语规整）
    """
    q = (model_question or "").strip()
    t = (transcript or "").strip()
    if not t:
        return q
    if not q:
        return t
    if _looks_fragment(q) and len(t) >= len(q):
        return t
    if len(t) >= len(q) + 2:
        return t
    # 转写覆盖模型（模型偶发截断）
    if t.startswith(q) and len(t) > len(q):
        return t
    return q


def with_calibrated_question(arguments: Any, transcript: str) -> Any:
    """写回 question，尽量保持原 arguments 形态（dict / JSON 字符串）。"""
    model_q = parse_tool_question(arguments)
    final_q = calibrate_question(model_question=model_q, transcript=transcript)
    if not final_q:
        return arguments

    if isinstance(arguments, dict):
        out = dict(arguments)
        out["question"] = final_q
        return out

    if isinstance(arguments, str):
        raw = arguments.strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return json.dumps({"question": final_q}, ensure_ascii=False)
        if isinstance(parsed, dict):
            parsed = dict(parsed)
            parsed["question"] = final_q
            return json.dumps(parsed, ensure_ascii=False)
        return json.dumps({"question": final_q}, ensure_ascii=False)

    return {"question": final_q}
