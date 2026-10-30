"""LiteLLM's custom auth (config.yaml `custom_auth`): a thin adapter that hands the request to
gate.py and returns a minimal key allowed on the one route, so LiteLLM's own route check is a
second line. The decision lives in gate.py."""

from pathlib import Path

from fastapi import Request
from gate import PATH, decide
from litellm.proxy._types import ProxyException, UserAPIKeyAuth

CREDENTIAL_FILE = Path("/run/secrets/agent_service_api_credential")
HTTP_UNAUTHORIZED = 401


# LiteLLM calls the hook with both arguments; the bearer is read from the request itself.
async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    verdict = decide(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        authorization=request.headers.get("authorization"),
        body=await request.body(),
        credential_file=CREDENTIAL_FILE,
    )
    if not verdict.allowed:
        raise ProxyException(
            message=verdict.reason, type="auth_error", param="authorization", code=HTTP_UNAUTHORIZED
        )
    return UserAPIKeyAuth(api_key="service", allowed_routes=[PATH])
