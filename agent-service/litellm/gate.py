"""The decision of LiteLLM's custom auth hook: pure and standard library only, so CI tests it case
by case without LiteLLM (tests/test_litellm_gate.py). auth.py is the thin adapter that hands it
the request.

A request passes only as `POST` on exactly /v1/chat/completions, with no query string (LiteLLM
reads `model` from the query too), the service credential's `shared_key` as the bearer, and a JSON
body whose keys are exactly those agent-runtime's one posting function sends. Anything else,
including a credential file that is missing or malformed, is refused. A reason never carries the
credential or the bearer.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from pathlib import Path

METHOD = "POST"
PATH = "/v1/chat/completions"
BODY_KEYS = frozenset({"model", "messages", "tools"})
CREDENTIAL_FIELDS = frozenset({"shared_key", "app_keys"})
BEARER_PREFIX = "Bearer "


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


def decide(
    *,
    method: str,
    path: str,
    query: str,
    authorization: str | None,
    body: bytes,
    credential_file: Path,
) -> Verdict:
    if method != METHOD or path != PATH:
        return Verdict(False, "only POST /v1/chat/completions is served")
    if query:
        return Verdict(False, "a query string is not accepted")
    shared_key = _shared_key(credential_file)
    if shared_key is None:
        return Verdict(False, "the service credential is missing or invalid")
    if not _bearer_matches(authorization, shared_key):
        return Verdict(False, "the bearer does not match")
    if _body_keys(body) != BODY_KEYS:
        return Verdict(False, "the body keys are not exactly model, messages and tools")
    return Verdict(True, "")


def _shared_key(credential_file: Path) -> str | None:
    try:
        document = json.loads(credential_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or not document.keys() <= CREDENTIAL_FIELDS:
        return None
    shared_key = document.get("shared_key")
    return shared_key if isinstance(shared_key, str) and shared_key else None


def _bearer_matches(authorization: str | None, shared_key: str) -> bool:
    if authorization is None or not authorization.startswith(BEARER_PREFIX):
        return False
    return hmac.compare_digest(
        authorization.removeprefix(BEARER_PREFIX).encode(), shared_key.encode()
    )


def _body_keys(body: bytes) -> frozenset[str] | None:
    try:
        document = json.loads(body)
    except ValueError:
        return None
    return frozenset(document) if isinstance(document, dict) else None
