"""得助 OpenAPI RSA 签名。"""

from __future__ import annotations

import base64
import re

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _normalize_pem(raw: str) -> bytes:
    text = (raw or "").strip()
    if "BEGIN" in text:
        return text.encode("utf-8")
    # 去掉空白后包成 PKCS#8
    body = re.sub(r"\s+", "", text)
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"
    return pem.encode("utf-8")


def load_private_key(raw: str):
    data = _normalize_pem(raw)
    return serialization.load_pem_private_key(data, password=None)


def rsa_sign(content: str, private_key_pem: str, mode: str = "sha1_pkcs1") -> str:
    """
    老鉴权: sign(appId + timestamp)
    新鉴权: signPss(appId + timestamp + params + body)
    """
    key = load_private_key(private_key_pem)
    payload = content.encode("utf-8")
    mode = (mode or "sha1_pkcs1").strip().lower()

    if mode in {"pss_sha256", "sha256_pss", "pss"}:
        signature = key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    elif mode in {"sha256_pkcs1", "sha256"}:
        signature = key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    else:
        # 默认老鉴权：SHA1withRSA
        signature = key.sign(payload, padding.PKCS1v15(), hashes.SHA1())

    return base64.b64encode(signature).decode("ascii")


def flatten_json_for_sign(value) -> str:
    """新鉴权 body 扁平化：key 升序、去空、无分隔。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(flatten_json_for_sign(v) for v in value if v is not None and v != "")
    if isinstance(value, dict):
        parts: list[str] = []
        for k in sorted(value.keys()):
            v = value[k]
            if v is None or v == "":
                continue
            parts.append(f"{k}{flatten_json_for_sign(v)}")
        return "".join(parts)
    return str(value)
