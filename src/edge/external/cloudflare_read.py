"""Cloudflare, read directly for the host check (#138): GET only, the read token only.

The one documented wire shape, from Cloudflare's published OpenAPI schema
(github.com/cloudflare/api-schemas, `openapi.json`, 19.10.2026):

- Every answer is `{"success": bool, "errors": [{"code": int, "message": str}], "result": ...}`.
  The lists are paged with `page` and `per_page` and answer `result_info` with `page`,
  `per_page`, `count`, `total_count` and `total_pages`.
- `GET /zones?name=`: `result[].id`, `result[].account.id`.
- `GET /zones/{id}/dns_records`: `result[].name`, `type`, `content`, `proxied`.
- `GET /accounts/{id}/cfd_tunnel?is_deleted=false`: `result[].id`.
- `GET /accounts/{id}/access/groups`: `result[].id`, `result[].name` (an include rule names a
  group by id only).
- `GET /accounts/{id}/access/apps`: `result[].type`; of every app `destinations[]` (`uri` of a
  `public` destination; `domain` and `self_hosted_domains` are deprecated and never read) and
  the embedded `policies[]` (`decision`, `include[]`), so no policies read exists. A rule of
  `include[]` is a map with the one key `everyone` or `group` (`{"id": ...}`).
- The schema lists `access_page` and `access_per_page` (default 1000, maximum 1000) for the apps
  and the groups (openapi.json of 19.10.2026); `page` and `per_page` are sent and answered.
  Paging fails closed: `result_info` must name the requested page and page size, the same
  `total_pages` and `total_count` on every page and a `count` equal to the page's results, and
  every page but the last must be full.
- An app that has destinations may carry only the fields of `UNDERSTOOD_APP_FIELDS`. Whether and
  how a path rule or override can make a path public is not documented in any schema this check
  could read (the pinned provider's resource schema, 5.28.0, has none), so a field beyond them
  is refused (fail closed) instead of ignored.

The token travels only in the `Authorization` header, set as an unredirected header on an opener
that follows no redirect and uses no environment proxy. No message names a body or the token.
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

BASE_URL = "https://api.cloudflare.com/client/v4"
PAGE_SIZE = 50
MAXIMUM_PAGES = 200
TIMEOUT_SECONDS = 20.0
MAXIMUM_BODY_BYTES = 16 * 1024 * 1024
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
SELF_HOSTED = "self_hosted"
PUBLIC_DESTINATION = "public"
# The attributes of `cloudflare_zero_trust_access_application` (provider 5.28.0, `tofu providers
# schema`, 19.10.2026) and the response-only metadata of the API object. The union of every app
# type, so one set serves them all.
UNDERSTOOD_APP_FIELDS = frozenset(
    {
        "id", "uid", "aud", "created_at", "updated_at", "name", "type", "domain", "domain_type",
        "destinations", "self_hosted_domains", "policies", "allow_authenticate_via_warp",
        "allow_iframe", "allowed_idps", "app_launcher_logo_url", "app_launcher_url",
        "app_launcher_visible", "auto_redirect_to_identity", "bg_color", "cors_headers",
        "custom_deny_message", "custom_deny_url", "custom_non_identity_deny_url", "custom_pages",
        "enable_binding_cookie", "footer_links", "header_bg_color", "http_only_cookie_attribute",
        "landing_page_design", "logo_url", "mfa_config", "oauth_configuration",
        "options_preflight_bypass", "path_cookie_attribute", "read_service_tokens_from_header",
        "saas_app", "same_site_cookie_attribute", "scim_config", "service_auth_401_redirect",
        "session_duration", "skip_app_launcher_login_page", "skip_interstitial", "tags",
        "target_criteria",
    }
)  # fmt: skip
ACCESS_NOT_ENABLED = (403, 9999, "access.api.error.not_enabled")


class CloudflareReadError(Exception):
    """A read failed; the message names the path, the status and the first Cloudflare error code."""


@dataclass(frozen=True)
class Record:
    name: str
    type: str
    content: str
    proxied: bool


@dataclass(frozen=True)
class Include:
    """One rule of a policy's `include`: `everyone`, a `group` with its id, or another kind."""

    kind: str
    group_id: str | None = None


@dataclass(frozen=True)
class Policy:
    decision: str
    include: tuple[Include, ...]


@dataclass(frozen=True)
class App:
    type: str
    destinations: tuple[str, ...]
    policies: tuple[Policy, ...]


@dataclass(frozen=True)
class AccessGroup:
    id: str
    name: str


@dataclass(frozen=True)
class Snapshot:
    """What the account holds. `apps` is None when Access is not enabled on it."""

    tunnel_id: str
    records: tuple[Record, ...]
    groups: tuple[AccessGroup, ...]
    apps: tuple[App, ...] | None


class CloudflareRead(Protocol):
    def read(self, zone: str) -> Snapshot: ...


class _Refusal(CloudflareReadError):
    def __init__(self, message: str, *, access_not_enabled: bool) -> None:
        super().__init__(message)
        self.access_not_enabled = access_not_enabled


class _DoNotFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_arguments: Any, **_keywords: Any) -> None:
        return None


class CloudflareClient:
    def __init__(
        self, token: str, base_url: str = BASE_URL, timeout_seconds: float = TIMEOUT_SECONDS
    ) -> None:
        _require_cloudflare_or_loopback(base_url)
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _DoNotFollow)

    def read(self, zone: str) -> Snapshot:
        zone_answer = self._one("/zones", {"name": zone}, "zone")
        account_id = _text(zone_answer.get("account"), "id", "/zones")
        zone_id = _text(zone_answer, "id", "/zones")
        tunnel_path = f"/accounts/{_segment(account_id)}/cfd_tunnel"
        tunnel = self._one(tunnel_path, {"is_deleted": "false"}, "tunnel")
        records_path = f"/zones/{_segment(zone_id)}/dns_records"
        groups_path = f"/accounts/{_segment(account_id)}/access/groups"
        apps = self._apps(f"/accounts/{_segment(account_id)}/access/apps")
        return Snapshot(
            tunnel_id=_text(tunnel, "id", tunnel_path),
            records=tuple(_record(item, records_path) for item in self._pages(records_path)),
            groups=() if apps is None else self._groups(groups_path),
            apps=apps,
        )

    def _groups(self, path: str) -> tuple[AccessGroup, ...]:
        return tuple(_group(item, path) for item in self._pages(path))

    def _apps(self, path: str) -> tuple[App, ...] | None:
        try:
            return tuple(_app(item, path) for item in self._pages(path))
        except _Refusal as refusal:
            if refusal.access_not_enabled:
                return None
            raise

    def _one(self, path: str, query: Mapping[str, str], what: str) -> dict:
        items = self._pages(path, query)
        if len(items) != 1:
            raise CloudflareReadError(f"{path}: expected exactly one {what}, found {len(items)}")
        return items[0]

    def _pages(self, path: str, query: Mapping[str, str] | None = None) -> list[dict]:
        items: list[dict] = []
        page, announced = 1, None
        while True:
            answer = self._get(path, {**(query or {}), "page": str(page), "per_page": PAGE_SIZE})
            page_items, totals = _checked_page(path, answer, page)
            if announced not in (None, totals):
                raise CloudflareReadError(f"{path}: the pages announce different totals")
            announced = totals
            total_pages, total_count = totals
            items += page_items
            if page >= total_pages:
                break
            page += 1
        if len(items) != total_count:
            raise CloudflareReadError(f"{path}: {len(items)} results of {total_count} announced")
        return items

    def _get(self, path: str, query: Mapping[str, str | int]) -> dict:
        request = urllib.request.Request(
            f"{self._base_url}{path}?{urllib.parse.urlencode(query)}", method="GET"
        )
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                status, body = response.status, response.read(MAXIMUM_BODY_BYTES + 1)
        except urllib.error.HTTPError as refused:
            with refused:
                status, body = refused.code, refused.read(MAXIMUM_BODY_BYTES + 1)
        except (OSError, ValueError, http.client.HTTPException) as failure:
            # Only the type: the text of a transport error can quote the request.
            raise CloudflareReadError(f"{path}: {type(failure).__name__}") from None
        return _answer(path, status, body)


def _require_cloudflare_or_loopback(base_url: str) -> None:
    """The bearer token goes to Cloudflare's own API or, in a test, to http on loopback."""
    parts = urllib.parse.urlsplit(base_url)
    on_loopback = parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTNAMES
    if base_url != BASE_URL and not on_loopback:
        raise ValueError(f"the Cloudflare base URL must be {BASE_URL} (or http on loopback)")


def _checked_page(path: str, answer: dict, page: int) -> tuple[list[dict], tuple[int, int]]:
    """The results of one page and the (total_pages, total_count) it announces."""
    page_items = answer.get("result")
    info = answer.get("result_info")
    if not isinstance(page_items, list) or not isinstance(info, dict):
        raise CloudflareReadError(f"{path}: the answer is not a paged list")
    if info.get("page") != page:
        raise CloudflareReadError(f"{path}: the answer is not page {page}")
    if info.get("per_page") != PAGE_SIZE:
        raise CloudflareReadError(f"{path}: the answer does not keep the page size {PAGE_SIZE}")
    total_pages = _count(info, "total_pages", path)
    total_count = _count(info, "total_count", path)
    if total_pages > MAXIMUM_PAGES:
        raise CloudflareReadError(f"{path}: more than {MAXIMUM_PAGES} pages")
    if _count(info, "count", path) != len(page_items) or len(page_items) > PAGE_SIZE:
        raise CloudflareReadError(f"{path}: page {page} holds other than the counted results")
    if page < total_pages and len(page_items) != PAGE_SIZE:
        raise CloudflareReadError(f"{path}: page {page} is not full but is not the last")
    if not all(isinstance(item, dict) for item in page_items):
        raise CloudflareReadError(f"{path}: the list holds something but maps")
    return page_items, (total_pages, total_count)


def _answer(path: str, status: int, body: bytes) -> dict:
    if len(body) > MAXIMUM_BODY_BYTES:
        raise CloudflareReadError(f"{path}: HTTP {status}, the answer is too large")
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        payload = None
    errors = payload.get("errors") if isinstance(payload, dict) else None
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    code = first.get("code") if isinstance(first.get("code"), int) else None
    if 200 <= status < 300 and isinstance(payload, dict) and payload.get("success") is True:
        return payload
    detail = f"Cloudflare error code {code}" if code is not None else "no Cloudflare error code"
    if not isinstance(payload, dict):
        detail = "malformed JSON answer"
    raise _Refusal(
        f"{path}: HTTP {status}, {detail}",
        access_not_enabled=(status, code, first.get("message")) == ACCESS_NOT_ENABLED,
    )


def _segment(identifier: str) -> str:
    return urllib.parse.quote(identifier, safe="")


def _count(info: dict, key: str, path: str) -> int:
    value = info.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CloudflareReadError(f"{path}: result_info.{key} is missing or not a count")
    return value


def _text(item: Any, key: str, path: str) -> str:
    value = item.get(key) if isinstance(item, dict) else None
    if not isinstance(value, str) or not value:
        raise CloudflareReadError(f"{path}: a result lacks its {key}")
    return value


def _list(item: dict, key: str, path: str) -> list:
    value = item.get(key)
    if not isinstance(value, list):
        raise CloudflareReadError(f"{path}: a result lacks its {key} list")
    return value


def _record(item: dict, path: str) -> Record:
    return Record(
        name=_text(item, "name", path).lower(),
        type=_text(item, "type", path),
        content=_text(item, "content", path),
        proxied=item.get("proxied") is True,
    )


def _group(item: dict, path: str) -> AccessGroup:
    return AccessGroup(id=_text(item, "id", path), name=_text(item, "name", path))


def _app(item: dict, path: str) -> App:
    app_type = _text(item, "type", path)
    required = app_type == SELF_HOSTED
    destinations = tuple(
        _text(destination, "uri", path).lower()
        for destination in _optional_list(item, "destinations", path, required)
        if isinstance(destination, dict)
        and destination.get("type", PUBLIC_DESTINATION) == PUBLIC_DESTINATION
    )
    if required or destinations:
        _require_understood_fields(item, path)
    return App(
        type=app_type,
        destinations=destinations,
        policies=tuple(
            _policy(policy, path) for policy in _optional_list(item, "policies", path, required)
        ),
    )


def _optional_list(item: dict, key: str, path: str, required: bool) -> list:
    return _list(item, key, path) if required or key in item else []


def _require_understood_fields(item: dict, path: str) -> None:
    unknown = sorted(set(item) - UNDERSTOOD_APP_FIELDS)
    if unknown:
        raise CloudflareReadError(
            f"{path}: an Access app carries fields this check does not understand: "
            f"{', '.join(map(str, unknown))}"
        )


def _policy(policy: Any, path: str) -> Policy:
    if not isinstance(policy, dict):
        raise CloudflareReadError(f"{path}: a policy is not embedded in its app")
    return Policy(
        decision=_text(policy, "decision", path),
        include=tuple(_include(rule, path) for rule in _list(policy, "include", path)),
    )


def _include(rule: Any, path: str) -> Include:
    if not isinstance(rule, dict) or len(rule) != 1:
        raise CloudflareReadError(f"{path}: an include rule is not a map with one key")
    (kind, body), *_ = rule.items()
    return Include(kind=kind, group_id=_text(body, "id", path) if kind == "group" else None)
