"""入库前脱敏（红线 D2 写入过门的第一道：脱敏）。

redact(text) -> (脱敏后文本, 命中类型列表)。命中的敏感片段替换为 [REDACTED:类型]。
命中检测在原始文本上做一次（避免前一条规则替换后掩盖后一条规则的命中），
替换再按规则顺序执行。
"""

import re

# (命中类型, 正则)。顺序即替换顺序：块级/长模式在前，避免被短模式截断。
_RULES: list[tuple[str, re.Pattern]] = [
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"(?i)\bapi[_-]?key\s*[=:]\s*[\"']?[A-Za-z0-9._~-]{8,}[\"']?")),
    ("sk_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("hex_secret", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("cookie", re.compile(r"(?im)^[ \t]*(?:Set-)?Cookie[ \t]*:[^\n]*$")),
]


def redact(text: str) -> tuple[str, list[str]]:
    """脱敏。返回 (redacted_text, 命中类型列表（按规则顺序去重）)。"""
    hits = [name for name, pattern in _RULES if pattern.search(text)]
    redacted = text
    for name, pattern in _RULES:
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return redacted, hits
