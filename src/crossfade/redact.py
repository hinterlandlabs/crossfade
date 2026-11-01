"""Secret redaction applied before anything is written to the artifact store or shown."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI/Anthropic-style keys
    re.compile(rb"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(rb"AKIA[0-9A-Z]{16}"),  # AWS access keys
    re.compile(rb"xox[baprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(rb"(?i)(\"?(?:api[_-]?key|secret|password|token|authorization)\"?\s*[:=]\s*\"?)([^\"\s,}]{6,})"),
]
_MASK = b"[REDACTED]"


def redact_bytes(data: bytes) -> bytes:
    for pat in _PATTERNS:
        if pat.groups >= 2:
            data = pat.sub(lambda m: m.group(1) + _MASK, data)
        else:
            data = pat.sub(_MASK, data)
    return data


def redact(text: str) -> str:
    return redact_bytes(text.encode()).decode(errors="replace")
