from __future__ import annotations

import hashlib


def redact_text(value: object) -> str:
    """Return a stable, non-reversible token for user-provided location text."""
    text = str(value or "")
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"len={len(text)} sha256={digest}"


def redact_route(origin: object, destination: object) -> str:
    return f"origin={redact_text(origin)} destination={redact_text(destination)}"
