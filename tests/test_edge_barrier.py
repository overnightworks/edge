"""The ingress barrier (#61 proof a): rendering, atomic write, verification and the git rules.
Nothing here starts a container or opens a socket; the checkout is always a `tmp_path`."""

import http.client
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from edge import barrier

REPOSITORY = Path(__file__).resolve().parents[1]
ADDRESS = "127.0.0.1:8081"
NOT_FOUND = barrier.Answer(404, b"404 page not found\n")
WHOAMI = barrier.Answer(200, b"Hostname: abc\n")
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
        host = name.removeprefix("switch-probe-")
        assert router["rule"] == f'Host(`{host}.{{{{ env "EDGE_DOMAIN" }}}}`)'


def test_the_deny_service_sends_the_request_back_to_traefik_without_its_host(checkout):
    """#61 rule 1 / proof a: the deny service sits in the file and drops the Host."""
    barrier_of(checkout, "hello").close()

    deny = parsed(checkout)["http"]["services"]["switch-probe-deny"]["loadBalancer"]

    assert deny["passHostHeader"] is False
    assert deny["servers"] == [{"url": f"http://{ADDRESS}"}]


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
        "Traefik did not answer 404 for hello.probe.invalid within 15 s (the barrier is written)"
    )
    assert ingress.present()


def test_a_404_with_another_body_is_no_proof_of_the_barrier(checkout):
    """#61 rule 3 / proof a: only Traefik's own 404 body proves the barrier."""
    other = barrier.Answer(404, b'{"error":"not found"}')

    with pytest.raises(barrier.BarrierError):
        barrier_of(checkout, "hello", fetch=lambda *_: other).close()


def test_opening_is_verified_only_once_the_host_stops_answering_the_barriers_404(checkout):
    """#61 rule 4 / proof a: opening is verified once the 404 is gone."""
    with pytest.raises(barrier.BarrierError, match=r"did not open hello\.probe\.invalid"):
        barrier_of(checkout, "hello", fetch=lambda *_: NOT_FOUND).open()


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


def test_a_never_ending_answer_is_cut_off_at_the_time_allowed():
    """#61 rule 3: a streaming body cannot hold the stop past its deadline."""
    server = socket.create_server(("127.0.0.1", 0))
    stop_serving = threading.Event()

    def trickle() -> None:
        connection, _ = server.accept()
        with connection:
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            while not stop_serving.is_set():
                try:
                    connection.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.3)

    serving = threading.Thread(target=trickle, daemon=True)
    serving.start()
    address = f"127.0.0.1:{server.getsockname()[1]}"
    try:
        started = time.monotonic()
        with pytest.raises(OSError):
            barrier.http_fetch(address, "hello.probe.invalid", 1.0)
        assert time.monotonic() - started < 2.0
    finally:
        stop_serving.set()
        server.close()
