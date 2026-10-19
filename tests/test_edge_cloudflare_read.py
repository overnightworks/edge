"""The Cloudflare read client (#138 cells R, rules 11-13): a loopback server, no network."""

import contextlib
import json
import socket
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from edge.external import cloudflare_read
from edge.external.cloudflare_read import (
    AccessGroup,
    App,
    CloudflareClient,
    CloudflareReadError,
    Include,
    Policy,
    Record,
    Snapshot,
)

TOKEN = "sentinel-token-4b1d-never-print-me"
ZONE = "example.test"
ZONE_ID = "zone-1"
ACCOUNT_ID = "account-1"
TUNNEL_ID = "11111111-2222-3333-4444-555555555555"
LOOPBACK = {"127.0.0.1", "::1"}
APPS_PATH = f"/accounts/{ACCOUNT_ID}/access/apps"
RECORDS_PATH = f"/zones/{ZONE_ID}/dns_records"
TUNNELS_PATH = f"/accounts/{ACCOUNT_ID}/cfd_tunnel"
GROUPS_PATH = f"/accounts/{ACCOUNT_ID}/access/groups"

Answer = tuple[int, dict | bytes]
Route = Callable[[dict[str, str]], Answer]


@pytest.fixture(autouse=True)
def no_connection_leaves_loopback(monkeypatch):
    """The backstop: whatever a test builds, a connection beyond loopback fails loudly."""
    real_connect = socket.socket.connect

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in LOOPBACK:
            raise AssertionError(f"a test connection leaves loopback: {address[0]}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


class FakeCloudflare:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str], str | None]] = []
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                outer.requests.append((parts.path, query, self.headers.get("Authorization")))
                status, body = outer.routes[parts.path](query)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                if status in (301, 302):
                    self.send_header("Location", outer.redirect_to)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):  # the client timed out and left
                    self.wfile.write(payload)

            def log_message(self, *_arguments) -> None:
                pass

        self.redirect_to = ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def client(self, **keywords) -> CloudflareClient:
        return CloudflareClient(TOKEN, base_url=self.url, **keywords)

    def serve(self, path: str, items: list, page_size: int = 2) -> None:
        self.routes[path] = lambda query: (200, page_of(items, int(query["page"]), page_size))

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def page_of(items: list, page: int, page_size: int) -> dict:
    total_pages = max(1, -(-len(items) // page_size))
    return ok(
        items[(page - 1) * page_size : page * page_size],
        page=page,
        total_pages=total_pages,
        total_count=len(items),
    )


def ok(result: list, **info) -> dict:
    return {"success": True, "errors": [], "result": result, "result_info": info}


def failure(code: int, message: str = "no") -> dict:
    return {"success": False, "errors": [{"code": code, "message": message}], "result": None}


def self_hosted(domain: str, decision: str, include: list) -> dict:
    return {
        "type": "self_hosted",
        "domain": domain,
        "destinations": [{"type": "public", "uri": domain}, {"type": "private", "cidr": "10/8"}],
        "policies": [{"id": "p", "decision": decision, "include": include}],
    }


RECORDS = [
    {"name": f"{label}.{ZONE}", "type": "CNAME", "content": "x", "proxied": True}
    for label in ("a", "b", "c")
]
APPS = [
    self_hosted(f"www.{ZONE}", "bypass", [{"everyone": {}}]),
    self_hosted(f"*.{ZONE}", "allow", [{"group": {"id": "g1"}}]),
    {"type": "app_launcher"},
]
GROUPS = [{"id": "g1", "name": "operator"}]


@pytest.fixture
def cloudflare() -> Iterator[FakeCloudflare]:
    fake = FakeCloudflare()
    fake.serve("/zones", [{"id": ZONE_ID, "name": ZONE, "account": {"id": ACCOUNT_ID}}])
    fake.serve(TUNNELS_PATH, [{"id": TUNNEL_ID}])
    fake.serve(RECORDS_PATH, RECORDS)
    fake.serve(GROUPS_PATH, GROUPS)
    fake.serve(APPS_PATH, APPS)
    yield fake
    fake.close()


def read_error(cloudflare: FakeCloudflare, **client_keywords) -> str:
    with pytest.raises(CloudflareReadError) as raised:
        cloudflare.client(**client_keywords).read(ZONE)
    return str(raised.value)


def test_the_snapshot_holds_every_page_and_each_request_carries_the_token_in_the_header_only(
    cloudflare,
):
    """R1."""
    assert cloudflare.client().read(ZONE) == Snapshot(
        tunnel_id=TUNNEL_ID,
        records=tuple(Record(f"{label}.{ZONE}", "CNAME", "x", True) for label in "abc"),
        groups=(AccessGroup("g1", "operator"),),
        apps=(
            App("self_hosted", (f"www.{ZONE}",), (Policy("bypass", (Include("everyone"),)),)),
            App("self_hosted", (f"*.{ZONE}",), (Policy("allow", (Include("group", "g1"),)),)),
            App("app_launcher", (), ()),
        ),
    )
    paths = [path for path, _, _ in cloudflare.requests]
    assert paths.count(RECORDS_PATH) == 2
    assert {header for _, _, header in cloudflare.requests} == {f"Bearer {TOKEN}"}
    assert TOKEN not in json.dumps([query for _, query, _ in cloudflare.requests])


def test_access_off_is_a_typed_answer_not_an_error(cloudflare):
    """R2."""
    cloudflare.routes[APPS_PATH] = lambda _query: (
        403,
        failure(9999, "access.api.error.not_enabled"),
    )
    assert cloudflare.client().read(ZONE).apps is None


@pytest.mark.parametrize(
    ("status", "body", "expected_in_message"),
    [
        pytest.param(
            403,
            failure(9999, "something else"),
            "HTTP 403, Cloudflare error code 9999",
            id="9999 but another message",
        ),
        pytest.param(
            403,
            failure(10000, "access.api.error.not_enabled"),
            "HTTP 403, Cloudflare error code 10000",
            id="the message under another code",
        ),
        pytest.param(
            401,
            failure(10000, "Authentication error"),
            "HTTP 401, Cloudflare error code 10000",
            id="401",
        ),
        pytest.param(500, failure(1000), "HTTP 500, Cloudflare error code 1000", id="500"),
        pytest.param(
            200, failure(7003), "HTTP 200, Cloudflare error code 7003", id="success false"
        ),
        pytest.param(200, b"{not json", "HTTP 200, malformed JSON answer", id="malformed JSON"),
        pytest.param(
            500,
            failure(1, f"echoes {TOKEN}"),
            "HTTP 500, Cloudflare error code 1",
            id="a body with the token",
        ),
        pytest.param(302, b"", "HTTP 302", id="a redirect status"),
    ],
)
def test_a_refused_apps_read_raises_naming_path_status_and_code_but_never_body_or_token(
    cloudflare, status, body, expected_in_message
):
    """R3 (and rule 7: only the exact 9999 answer is Access off)."""
    cloudflare.routes[APPS_PATH] = lambda _query: (status, body)
    message = read_error(cloudflare)
    assert message.startswith(f"{APPS_PATH}: {expected_in_message}")
    assert TOKEN not in message
    assert "echoes" not in message


@pytest.mark.parametrize(
    ("path", "items", "expected"),
    [
        pytest.param("/zones", [], "exactly one zone, found 0", id="no zone"),
        pytest.param("/zones", [{"id": "z"}] * 2, "exactly one zone, found 2", id="two zones"),
        pytest.param(
            TUNNELS_PATH, [{"id": "t"}] * 2, "exactly one tunnel, found 2", id="two tunnels"
        ),
        pytest.param(TUNNELS_PATH, [], "exactly one tunnel, found 0", id="no tunnel"),
    ],
)
def test_zero_or_several_zones_or_tunnels_are_an_error(cloudflare, path, items, expected):
    """R3."""
    cloudflare.serve(path, items)
    assert expected in read_error(cloudflare)


def test_a_timeout_is_an_error_without_a_body(cloudflare):
    """R3."""

    def hang(_query: dict[str, str]) -> Answer:
        cloudflare.release.wait(10)
        return 200, {}

    cloudflare.routes["/zones"] = hang
    message = read_error(cloudflare, timeout_seconds=0.2)
    assert message.startswith("/zones: ") and TOKEN not in message


def test_a_redirect_is_not_followed_and_the_token_never_reaches_its_target(cloudflare):
    """R4."""
    elsewhere = FakeCloudflare()
    elsewhere.routes["/zones"] = lambda _query: (200, ok([]))
    cloudflare.redirect_to = f"{elsewhere.url}/zones"
    cloudflare.routes["/zones"] = lambda _query: (302, b"")
    try:
        message = read_error(cloudflare)
    finally:
        elsewhere.close()
    assert message.startswith("/zones: HTTP 302")
    assert elsewhere.requests == []


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
def test_the_environment_proxy_is_not_used(cloudflare, monkeypatch, variable):
    """R4."""
    monkeypatch.setenv(variable, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    assert cloudflare.client().read(ZONE).tunnel_id == TUNNEL_ID
    assert cloudflare.requests


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            lambda page: {"success": True, "errors": [], "result": []}, id="no result_info"
        ),
        pytest.param(lambda page: ok([{}], page=page, total_count=1), id="no total_pages"),
        pytest.param(lambda page: ok([{}], page=page, total_pages=1), id="no total_count"),
        pytest.param(
            lambda page: ok([{}], page=page, total_pages=1, total_count=3), id="fewer than counted"
        ),
        pytest.param(
            lambda page: ok([{}], page=1, total_pages=3, total_count=3), id="the same page again"
        ),
    ],
)
def test_pagination_fails_closed(cloudflare, answer):
    """R5."""
    cloudflare.routes[APPS_PATH] = lambda query: (200, answer(int(query["page"])))
    assert read_error(cloudflare).startswith(APPS_PATH)


def test_a_failing_middle_page_is_an_error_not_a_short_snapshot(cloudflare):
    """R5."""

    def third_pages(query: dict[str, str]) -> Answer:
        if query["page"] == "2":
            return 500, failure(1000)
        return 200, ok(
            [{"type": "app_launcher"}], page=int(query["page"]), total_pages=3, total_count=3
        )

    cloudflare.routes[APPS_PATH] = third_pages
    assert "HTTP 500" in read_error(cloudflare)


def test_only_the_live_tunnel_is_asked_for(cloudflare):
    """R6."""
    cloudflare.routes[TUNNELS_PATH] = lambda query: (
        200,
        page_of(
            [{"id": TUNNEL_ID}] if query["is_deleted"] == "false" else [{"id": "gone"}] * 2, 1, 5
        ),
    )
    assert cloudflare.client().read(ZONE).tunnel_id == TUNNEL_ID


@pytest.mark.parametrize(
    "base_url",
    ["http://example.com", "ftp://127.0.0.1", "http://127.0.0.1.example.com"],
)
def test_the_token_goes_only_over_https_or_to_loopback(base_url):
    with pytest.raises(ValueError):
        CloudflareClient(TOKEN, base_url=base_url)


def test_the_default_base_url_is_cloudflares_https_api():
    assert cloudflare_read.BASE_URL == "https://api.cloudflare.com/client/v4"


def test_the_backstop_refuses_a_connection_beyond_loopback():
    with pytest.raises(AssertionError, match="leaves loopback"):
        socket.create_connection(("192.0.2.1", 9), timeout=0.1)
