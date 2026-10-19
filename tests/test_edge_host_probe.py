"""The host probe adapter (#154 cells PB1-PB3, PB10): a loopback server and injected connections."""

import errno
import http.client
import socket
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from edge.external import host_probe
from edge.external.host_probe import Answer, FailureKind, HttpProber, Unreachable

pytestmark = pytest.mark.usefixtures("loopback_only")

SENTINEL = "sentinel-7c2e-never-quote-me"
FQDN = "www.example.test"
TEAM_LOGIN = "https://team.cloudflareaccess.com/cdn-cgi/access/login/abc"
SHORT_TIMEOUT = 0.3


class Behaviour:
    """What the loopback server answers; `release` ends a body that never finishes."""

    def __init__(self, status=200, headers=(), stall_seconds=0.0, announce_body=False):
        self.status = status
        self.headers = list(headers)
        self.stall_seconds = stall_seconds
        self.announce_body = announce_body
        self.requests: list[dict] = []
        self.release = threading.Event()


@contextmanager
def serving(behaviour: Behaviour):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            behaviour.requests.append(
                {
                    "line": self.requestline,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            if behaviour.stall_seconds:
                behaviour.release.wait(behaviour.stall_seconds)
            self.send_response(behaviour.status)
            for name, value in behaviour.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", "100" if behaviour.announce_body else "0")
            self.end_headers()
            if behaviour.announce_body:
                behaviour.release.wait(5)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        behaviour.release.set()
        server.shutdown()
        server.server_close()


def ask(behaviour: Behaviour, timeout: float = host_probe.REQUEST_TIMEOUT_SECONDS):
    with serving(behaviour) as origin:
        return HttpProber(origin, timeout=timeout).ask(FQDN)


RAY = ("CF-RAY", "8a1b2c3d-FRA")


@pytest.mark.parametrize(
    ("behaviour", "expected"),
    [
        pytest.param(
            Behaviour(302, [RAY, ("Location", TEAM_LOGIN)]),
            Answer(302, TEAM_LOGIN, True, False),
            id="access-redirect-absolute",
        ),
        pytest.param(
            Behaviour(302, [RAY, ("Location", "/cdn-cgi/access/login/abc")]),
            Answer(302, "/cdn-cgi/access/login/abc", True, False),
            id="access-redirect-relative",
        ),
        pytest.param(Behaviour(200, [RAY]), Answer(200, None, True, False), id="200"),
        pytest.param(Behaviour(404, [RAY]), Answer(404, None, True, False), id="404"),
        pytest.param(Behaviour(502, [RAY]), Answer(502, None, True, False), id="502"),
        pytest.param(Behaviour(200), Answer(200, None, False, False), id="no-cf-ray"),
        pytest.param(
            Behaviour(403, [RAY, ("cf-mitigated", "challenge")]),
            Answer(403, None, True, True),
            id="challenge",
        ),
        pytest.param(
            Behaviour(403, [("cf-ray", "x"), ("CF-Mitigated", "CHALLENGE")]),
            Answer(403, None, True, True),
            id="challenge-upper-case",
        ),
        pytest.param(
            Behaviour(403, [RAY, ("cf-mitigated", "challenge ")]),
            Answer(403, None, True, True),
            id="challenge-trailing-space",
        ),
        pytest.param(
            Behaviour(403, [RAY, ("cf-mitigated", "\tChallenge")]),
            Answer(403, None, True, True),
            id="challenge-leading-tab-capital",
        ),
        pytest.param(
            Behaviour(403, [RAY, ("cf-mitigated", "challenged")]),
            Answer(403, None, True, False),
            id="challenged-is-not-a-challenge",
        ),
    ],
)
def test_the_answer_comes_back_typed(behaviour, expected):
    """PB1."""
    assert ask(behaviour) == expected


def test_a_redirect_is_not_followed():
    """PB1: the second server saw nothing."""
    elsewhere = Behaviour(200, [RAY])
    with serving(elsewhere) as other_origin:
        answer = ask(Behaviour(302, [RAY, ("Location", other_origin + "/")]))
    assert answer == Answer(302, other_origin + "/", True, False)
    assert elsewhere.requests == []


def test_a_refused_connection_is_unreachable():
    """PB1."""
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{closed_port.getsockname()[1]}"
    assert HttpProber(origin).ask(FQDN) == Unreachable(FailureKind.REFUSED)


def test_a_slow_answer_is_unreachable_by_timeout():
    """PB1."""
    assert ask(Behaviour(200, [RAY], stall_seconds=5), SHORT_TIMEOUT) == Unreachable(
        FailureKind.TIMEOUT
    )


def test_the_request_is_one_bare_get_for_the_host():
    """PB2."""
    behaviour = Behaviour(200, [RAY])
    ask(behaviour)
    (request,) = behaviour.requests
    assert request["line"] == "GET / HTTP/1.1"
    assert request["headers"]["host"] == FQDN
    assert request["headers"]["user-agent"] == host_probe.USER_AGENT
    assert not {"authorization", "cookie", "proxy-authorization"} & set(request["headers"])


def test_no_proxy_variable_changes_the_request(monkeypatch):
    """PB2: the proxy variables point at a dead port and the answer still arrives."""
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(variable, "http://127.0.0.1:9")
    assert ask(Behaviour(200, [RAY])) == Answer(200, None, True, False)


@pytest.mark.parametrize(
    "origin", ["http://example.com", "https://127.0.0.1:8443", "http://192.0.2.1:80"]
)
def test_a_test_origin_must_be_http_on_a_loopback_name(origin):
    """PB2."""
    with pytest.raises(ValueError, match="loopback"):
        HttpProber(origin)


class ScriptedConnection:
    """The transport boundary: raises `failure` at `stage`, otherwise answers headers only."""

    def __init__(self, failure=None, stage="getresponse", close_failure=None):
        self.failure, self.stage, self.close_failure = failure, stage, close_failure
        self.closed = False

    def request(self, *_args, **_kwargs):
        self._maybe_fail("request")

    def getresponse(self):
        self._maybe_fail("getresponse")
        return self

    def _maybe_fail(self, stage):
        if self.failure is not None and stage == self.stage:
            raise self.failure

    status = 200

    def getheader(self, name):
        return "ray" if name == "cf-ray" else None

    def close(self):
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


FAILURES = [
    (socket.gaierror(socket.EAI_NONAME, SENTINEL), FailureKind.DNS),
    (ssl.SSLCertVerificationError(1, SENTINEL), FailureKind.TLS),
    (ssl.SSLError(SENTINEL), FailureKind.TLS),
    (ConnectionRefusedError(SENTINEL), FailureKind.REFUSED),
    (ConnectionResetError(SENTINEL), FailureKind.DISCONNECTED),
    (http.client.RemoteDisconnected(SENTINEL), FailureKind.DISCONNECTED),
    (http.client.IncompleteRead(b"", 10), FailureKind.DISCONNECTED),
    (TimeoutError(SENTINEL), FailureKind.TIMEOUT),
    (http.client.BadStatusLine(SENTINEL), FailureKind.MALFORMED),
    (http.client.LineTooLong(SENTINEL), FailureKind.MALFORMED),
    (OSError(errno.ENETUNREACH, SENTINEL), FailureKind.OTHER),
    (OSError(errno.EHOSTUNREACH, SENTINEL), FailureKind.OTHER),
]


@pytest.mark.parametrize("stage", ["factory", "request", "getresponse"])
@pytest.mark.parametrize(
    ("failure", "kind"),
    FAILURES,
    ids=[type(f).__name__ + str(i) for i, (f, _) in enumerate(FAILURES)],
)
def test_every_transport_failure_becomes_one_fixed_phrase_and_closes_the_connection(
    failure, kind, stage
):
    """PB3, PB10."""
    connection = ScriptedConnection(failure, stage)

    def factory(_name, _timeout):
        if stage == "factory":
            raise failure
        return connection

    outcome = HttpProber(connection=factory).ask(FQDN)
    assert outcome == Unreachable(kind)
    assert SENTINEL not in str(outcome.kind)
    assert connection.closed == (stage != "factory")


def test_the_connection_is_closed_after_a_success_too():
    """PB3."""
    connection = ScriptedConnection()
    outcome = HttpProber(connection=lambda _name, _timeout: connection).ask(FQDN)
    assert outcome == Answer(200, None, True, False)
    assert connection.closed


@pytest.mark.parametrize(
    ("failure", "decided"),
    [
        pytest.param(None, Answer(200, None, True, False), id="after-an-answer"),
        pytest.param(
            TimeoutError(SENTINEL), Unreachable(FailureKind.TIMEOUT), id="after-a-failure"
        ),
    ],
)
def test_a_failing_close_keeps_the_verdict_already_decided(failure, decided):
    """PB3."""
    connection = ScriptedConnection(failure, close_failure=OSError(SENTINEL))
    assert HttpProber(connection=lambda _name, _timeout: connection).ask(FQDN) == decided
    assert connection.closed


def test_an_interrupt_during_close_propagates():
    """PB3."""
    connection = ScriptedConnection(close_failure=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        HttpProber(connection=lambda _name, _timeout: connection).ask(FQDN)


def test_the_two_unreachable_network_errors_share_the_generic_phrase():
    """PB3."""
    assert FailureKind.OTHER.value == "connection failure"


def test_complete_headers_return_without_reading_the_body():
    """PB3: the body is announced and never sent; reading it would time out."""
    behaviour = Behaviour(200, [RAY], announce_body=True)
    assert ask(behaviour, timeout=2) == Answer(200, None, True, False)


def test_a_long_hostile_header_is_carried_as_data_not_raised():
    """PB10: the adapter hands the value on; no finding quotes it (see the host check cells)."""
    behaviour = Behaviour(302, [RAY, ("Location", "\x01" + SENTINEL * 500)])
    answer = ask(behaviour)
    assert isinstance(answer, Answer)
    assert answer.status == 302


@pytest.mark.parametrize(
    ("location", "is_access"),
    [
        pytest.param("/x", True, id="one-slash-is-on-the-probed-host"),
        pytest.param("//evil.example/x", False, id="two-slashes-name-another-authority"),
        pytest.param("///evil.example/x", False, id="three-slashes"),
        pytest.param("/\\evil.example/x", False, id="slash-backslash"),
    ],
)
def test_only_a_single_leading_slash_is_root_relative(location, is_access):
    """PB4: probed on an Access team host, where any location on the probed host counts."""
    answer = Answer(302, location, True, False)
    assert host_probe.is_access_redirect(answer, "team.cloudflareaccess.com") is is_access
