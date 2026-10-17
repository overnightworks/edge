"""The ingress barrier (#61 proof a): rendering, atomic write, verification and the git rules.
Nothing here starts a container; the checkout is always a `tmp_path`. Sockets appear only in the
last section, where `http_fetch` meets a real local TCP server that misbehaves."""

import http.client
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from edge import barrier

REPOSITORY = Path(__file__).resolve().parents[1]
ADDRESS = "127.0.0.1:8081"
NOT_FOUND = barrier.Answer(404, b"404 page not found\n", "probe")  # the barrier of unit `probe`
PLAIN_NOT_FOUND = barrier.Answer(404, b"404 page not found\n")  # Traefik's ordinary no-route answer
WHOAMI = barrier.Answer(200, b"Hostname: abc\n")
TIME_ALLOWED = 0.3
HOST = "hello.probe.invalid"
Script = Callable[[socket.socket, threading.Event], None]
KEYCLOAK_RULE = "Host(`auth.probe.invalid`) && PathPrefix(`/realms`)"


def config() -> dict:
    return {
        "services": {
            "traefik": {"environment": {"EDGE_DOMAIN": "probe.invalid"}},
            "keycloak": {
                "labels": {
                    "traefik.enable": "true",
                    "edge.name": "auth",
                    "traefik.http.routers.auth.rule": KEYCLOAK_RULE,
                }
            },
            "keycloak-db": {},
            "hello": {"labels": {"traefik.enable": "true", "edge.name": "hello"}},
            "hello-twin": {"labels": {"traefik.enable": "true", "edge.name": "hello"}},
            "internal": {"labels": {"traefik.enable": "false", "edge.name": "internal"}},
            "stray": {"labels": {"traefik.enable": "true", "edge.name": "Not_Valid"}},
            "unnamed": {"labels": {"traefik.enable": "true"}},
        }
    }


class Clock:
    """Time that only moves when the verifier sleeps, so a 15 s wait costs nothing."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def verifier(fetch: Callable[[str, str, float], barrier.Answer]) -> barrier.Verifier:
    clock = Clock()
    return barrier.Verifier(fetch=fetch, clock=clock, sleep=clock.sleep)


def refused(_address: str, _host: str, _seconds: float) -> barrier.Answer:
    raise ConnectionRefusedError


def garbled(_address: str, _host: str, _seconds: float) -> barrier.Answer:
    raise http.client.IncompleteRead(b"404 page")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    (tmp_path / "dynamic").mkdir()
    (tmp_path / "traefik.yml").write_text(f'entryPoints:\n  edge:\n    address: "{ADDRESS}"\n')
    return tmp_path


def barrier_of(checkout: Path, *services: str, fetch=lambda *_: NOT_FOUND) -> barrier.Barrier:
    return barrier.Barrier.of_unit(config(), services, checkout, "probe", verifier(fetch))


def parsed(checkout: Path) -> dict:
    return yaml.safe_load((checkout / "dynamic" / "switch-probe.yml").read_text())


def test_one_router_per_routed_host_on_the_edge_entrypoint_only(checkout):
    """#61 rule 1 / proof a: one router per routed host, on `edge` only."""
    barrier_of(checkout, "keycloak", "hello", "hello-twin", "keycloak-db").close()

    document = parsed(checkout)

    assert sorted(document["http"]["routers"]) == ["switch-probe-auth", "switch-probe-hello"]
    for name, router in document["http"]["routers"].items():
        assert router["entryPoints"] == ["edge"]
        assert router["priority"] == 1_000_000
        assert router["service"] == "switch-probe-deny"
        assert router["middlewares"] == ["switch-probe-mark"]
        host = name.removeprefix("switch-probe-")
        assert router["rule"] == f'Host(`{host}.{{{{ env "EDGE_DOMAIN" }}}}`)'


def test_the_deny_service_sends_the_request_back_to_traefik_without_its_host(checkout):
    """#61 rule 1 / proof a: the deny service sits in the file and drops the Host."""
    barrier_of(checkout, "hello").close()

    deny = parsed(checkout)["http"]["services"]["switch-probe-deny"]["loadBalancer"]

    assert deny["passHostHeader"] is False
    assert deny["servers"] == [{"url": f"http://{ADDRESS}"}]


def test_the_barrier_marks_its_answer_with_the_unit_by_a_middleware_in_the_same_file(checkout):
    """#61 rule 1 / proof a: the same-file middleware adds `X-Edge-Barrier: <unit>`."""
    barrier_of(checkout, "hello").close()

    mark = parsed(checkout)["http"]["middlewares"]["switch-probe-mark"]

    assert mark == {"headers": {"customResponseHeaders": {"X-Edge-Barrier": "probe"}}}


def test_an_explicit_label_router_is_barred_by_its_host_alone(checkout):
    """#61 rule 1 / proof a: keycloak's PathPrefix label router is barred by Host alone."""
    barrier_of(checkout, "keycloak").close()

    rule = parsed(checkout)["http"]["routers"]["switch-probe-auth"]["rule"]

    assert "PathPrefix" not in rule
    assert rule.startswith("Host(`auth.")


@pytest.mark.parametrize("spelling", ["1", "t", "T", "TRUE", "True"])
def test_every_spelling_traefik_reads_as_true_gets_a_barrier(checkout, spelling):
    """#61 rule 1: Traefik parses `traefik.enable` with Go's ParseBool, and so does the barrier."""
    services = {"labels": {"traefik.enable": spelling, "edge.name": "hello"}}
    ingress = barrier.Barrier.of_unit(
        {"services": {**config()["services"], "spelled": services}},
        ["spelled"],
        checkout,
        "probe",
        verifier(lambda *_: NOT_FOUND),
    )

    ingress.close()

    assert "switch-probe-hello" in parsed(checkout)["http"]["routers"]


@pytest.mark.parametrize("service", ["keycloak-db", "internal", "stray", "unnamed"])
def test_a_unit_without_a_routed_host_gets_no_file_and_is_never_asked(checkout, service):
    """#61 rule 1 / proof a: no routed service, no file and no request."""
    ingress = barrier_of(checkout, service, fetch=refused)

    ingress.close()
    ingress.open()

    assert not list((checkout / "dynamic").iterdir())


def test_the_file_is_flat_and_leaves_no_temporary_name(checkout):
    """#61 rule 1 / proof a: the atomic write leaves no `.tmp`."""
    barrier_of(checkout, "hello").close()

    assert [path.name for path in (checkout / "dynamic").iterdir()] == ["switch-probe.yml"]


def test_closing_twice_renders_the_same_file(checkout):
    """#61 rule 3 / proof a: a second close renders the same file."""
    ingress = barrier_of(checkout, "hello")
    ingress.close()
    first = ingress.path.read_text()

    ingress.close()

    assert ingress.path.read_text() == first


def test_opening_removes_the_file_and_is_idempotent(checkout):
    """#61 rule 4 / proof a: removal is idempotent."""
    ingress = barrier_of(checkout, "hello", fetch=lambda *_: WHOAMI)
    ingress.path.write_text("leftover")

    ingress.open()
    ingress.open()

    assert not ingress.present()


def test_a_write_that_fails_leaves_no_temporary_file(checkout):
    """#61 rule 1 / proof a: a failed write leaves no `.tmp`."""
    ingress = barrier_of(checkout, "hello")
    ingress.path.mkdir()  # the rename onto a directory fails

    with pytest.raises(barrier.BarrierError, match=r"cannot write switch-probe\.yml"):
        ingress.close()

    assert not ingress.path.with_name("switch-probe.yml.tmp").exists()


def test_the_barrier_is_verified_for_every_host_at_the_address_of_traefik_yml(checkout):
    """#61 rule 3 / proof a: every host is asked at the address of traefik.yml."""
    asked = []

    def fetch(address: str, host: str, _seconds: float) -> barrier.Answer:
        asked.append((address, host))
        return NOT_FOUND

    barrier_of(checkout, "keycloak", "hello", fetch=fetch).close()

    assert asked == [(ADDRESS, "auth.probe.invalid"), (ADDRESS, "hello.probe.invalid")]


def test_a_barrier_that_answers_404_only_after_a_while_is_verified(checkout):
    """#61 rule 3 / proof a: the verification polls until Traefik answers 404."""
    answers = iter([WHOAMI, refused, garbled, NOT_FOUND])

    def fetch(address: str, host: str, seconds: float) -> barrier.Answer:
        answer = next(answers)
        return answer(address, host, seconds) if callable(answer) else answer

    barrier_of(checkout, "hello", fetch=fetch).close()


@pytest.mark.parametrize(
    "fetch", [lambda *_: WHOAMI, refused, garbled], ids=["answers 200", "refused", "garbled"]
)
def test_a_barrier_traefik_does_not_answer_404_for_is_not_verified_after_15_seconds(
    checkout, fetch
):
    """#61 rule 3 / proof a: 200, refused or a broken answer for 15 s is not verified."""
    ingress = barrier_of(checkout, "hello", fetch=fetch)

    with pytest.raises(barrier.BarrierError) as failure:
        ingress.close()

    assert str(failure.value) == (
        "the barrier is written but Traefik did not answer the barrier's 404 "
        "(X-Edge-Barrier: probe) for hello.probe.invalid within 15 s"
    )
    assert ingress.present()


def test_a_404_with_another_body_is_no_proof_of_the_barrier(checkout):
    """#61 rule 3 / proof a: only Traefik's own 404 body proves the barrier."""
    other = barrier.Answer(404, b'{"error":"not found"}', "probe")
    ingress = barrier_of(checkout, "hello", fetch=lambda *_: other)

    with pytest.raises(barrier.BarrierError):
        ingress.close()


def test_a_marked_answer_with_another_status_is_no_proof_of_the_barrier(checkout):
    """#61 rule 3 / proof a: only status 404 proves the barrier, mark and body notwithstanding."""
    other = barrier.Answer(200, PLAIN_NOT_FOUND.body, "probe")
    ingress = barrier_of(checkout, "hello", fetch=lambda *_: other)

    with pytest.raises(barrier.BarrierError):
        ingress.close()


def test_a_plain_404_without_the_mark_is_no_proof_of_the_barrier(checkout):
    """#61 rule 3 / proof a: Traefik's no-route 404 is what a rejected file leaves, not proof."""
    ingress = barrier_of(checkout, "hello", fetch=lambda *_: PLAIN_NOT_FOUND)

    with pytest.raises(barrier.BarrierError):
        ingress.close()


def test_the_mark_of_another_unit_is_no_proof_of_this_barrier(checkout):
    """#61 rule 3 / proof a: the mark must name this unit."""
    other = barrier.Answer(404, PLAIN_NOT_FOUND.body, "other")
    ingress = barrier_of(checkout, "hello", fetch=lambda *_: other)

    with pytest.raises(barrier.BarrierError):
        ingress.close()


def test_opening_is_verified_only_once_the_host_stops_answering_the_barriers_404(checkout):
    """#61 rule 4 / proof a: opening is verified once the mark is gone."""
    ingress = barrier_of(checkout, "hello", fetch=lambda *_: NOT_FOUND)

    with pytest.raises(barrier.BarrierError, match=r"did not open hello\.probe\.invalid"):
        ingress.open()


@pytest.mark.parametrize(
    "answer",
    [PLAIN_NOT_FOUND, WHOAMI, barrier.Answer(302, b"")],
    ids=["a path-prefixed router's plain 404", "whoami", "a redirect"],
)
def test_opening_is_verified_by_any_answer_without_the_mark(checkout, answer):
    """#61 rule 4 / proof a: keycloak's `auth` answers `/` with a plain 404 once opened."""
    barrier_of(checkout, "keycloak", fetch=lambda *_: answer).open()


def test_the_host_domain_is_the_one_compose_gives_traefik():
    """#61 rule 1 / proof a: the domain is the one Compose gives traefik."""
    broken = {"services": {"traefik": {}, **{"hello": config()["services"]["hello"]}}}

    with pytest.raises(barrier.BarrierError, match="no EDGE_DOMAIN"):
        barrier.Barrier.of_unit(broken, ["hello"], REPOSITORY, "probe")


def test_the_address_is_read_from_the_real_traefik_yml():
    """#61 rule 3 / proof a: the address comes from the real traefik.yml."""
    assert barrier.edge_address(REPOSITORY) == ADDRESS


def test_the_barrier_names_are_ignored_by_git_but_the_directory_stays_tracked():
    """#61 rule 2 / proof a: `switch-*` and its `.tmp` are ignored; `.gitkeep` stays."""
    for name in ("switch-keycloak.yml", "switch-keycloak.yml.tmp"):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", f"dynamic/{name}"], cwd=REPOSITORY, check=False
        )
        assert ignored.returncode == 0, name
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "dynamic/.gitkeep"],
        cwd=REPOSITORY, capture_output=True, check=False,
    )  # fmt: skip
    assert tracked.returncode == 0
    hand_written = subprocess.run(
        ["git", "check-ignore", "-q", "dynamic/routes.yml"], cwd=REPOSITORY, check=False
    )
    assert hand_written.returncode == 1


def test_no_request_is_given_more_time_than_the_15_seconds_leave(checkout):
    """#61 rule 3: the verification ends within 15 s, whatever one request would take."""
    allowed = []
    clock = Clock()

    def slow(_address: str, _host: str, seconds: float) -> barrier.Answer:
        allowed.append(seconds)
        clock.now += seconds  # a request that uses all the time it is given
        raise TimeoutError

    ingress = barrier.Barrier.of_unit(
        config(),
        ["hello"],
        checkout,
        "probe",
        barrier.Verifier(fetch=slow, clock=clock, sleep=clock.sleep),
    )

    with pytest.raises(barrier.BarrierError):
        ingress.close()

    assert clock.now <= barrier.VERIFY_SECONDS + 0.5
    assert max(allowed) <= 3.0


@pytest.fixture
def serve() -> Iterator[Callable[[Script], str]]:
    """Start a server that runs `script` on its first connection; teardown ends and joins it."""
    server = socket.create_server(("127.0.0.1", 0))
    ended = threading.Event()
    workers: list[threading.Thread] = []

    def start(script: Script) -> str:
        def work() -> None:
            connection, _ = server.accept()
            with connection:
                script(connection, ended)

        workers.append(threading.Thread(target=work, daemon=True))
        workers[-1].start()
        return f"127.0.0.1:{server.getsockname()[1]}"

    yield start
    ended.set()
    server.close()  # an accept still waiting fails and ends its worker
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()


def trickle(connection: socket.socket, ended: threading.Event, head: bytes) -> None:
    """Send `head`, then one more byte as soon as the last was taken, until the test ends."""
    connection.recv(4096)
    connection.sendall(head)
    while not ended.is_set():
        try:
            connection.sendall(b"x")
        except OSError:
            return
        ended.wait(0.02)


def test_headers_that_trickle_in_are_cut_off_at_the_time_allowed(serve):
    """#61 rule 3: trickled headers cannot hold the stop past its deadline."""
    address = serve(lambda connection, ended: trickle(connection, ended, b"HTTP/1.1 200 OK\r\nX: "))

    with pytest.raises(OSError):
        barrier.http_fetch(address, HOST, TIME_ALLOWED)


def test_a_body_that_trickles_in_is_cut_off_at_the_time_allowed(serve):
    """#61 rule 3: a marked answer with a never-ending body cannot hold the stop either."""
    head = b"HTTP/1.1 404 Not Found\r\nX-Edge-Barrier: probe\r\nContent-Length: 1000000\r\n\r\n"
    address = serve(lambda connection, ended: trickle(connection, ended, head))

    with pytest.raises(OSError):
        barrier.http_fetch(address, HOST, TIME_ALLOWED)


def test_chunk_framing_that_trickles_in_is_cut_off_at_the_time_allowed(serve):
    """#61 rule 3: the framing of a chunked body is read within the deadline too."""
    head = b"HTTP/1.1 404 Not Found\r\nX-Edge-Barrier: probe\r\nTransfer-Encoding: chunked\r\n\r\n"
    address = serve(lambda connection, ended: trickle(connection, ended, head))

    with pytest.raises(OSError):
        barrier.http_fetch(address, HOST, TIME_ALLOWED)


def test_an_unmarked_answer_is_complete_once_its_headers_are_in(serve):
    """#61 rule 4: a streaming 200 whose body never comes still shows the mark is absent."""

    def headers_only(connection: socket.socket, ended: threading.Event) -> None:
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
        ended.wait()

    answer = barrier.http_fetch(serve(headers_only), HOST, TIME_ALLOWED)

    assert answer.status == 200
    assert answer.is_unmarked


def test_a_marked_answer_carries_its_status_body_and_mark(serve):
    """#61 rule 3: the barrier's own answer is read whole."""

    def barrier_answer(connection: socket.socket, ended: threading.Event) -> None:
        connection.recv(4096)
        connection.sendall(
            b"HTTP/1.1 404 Not Found\r\nX-Edge-Barrier: probe\r\nContent-Length: 19\r\n\r\n"
            b"404 page not found\n"
        )
        ended.wait()

    answer = barrier.http_fetch(serve(barrier_answer), HOST, TIME_ALLOWED)

    assert answer.is_barrier_of("probe")
