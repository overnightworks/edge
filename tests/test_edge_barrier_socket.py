"""The transport of the barrier's verification (#61 rule 3, rule 4): `http_fetch` against a real
local TCP server that misbehaves. Pure logic is in test_edge_barrier.py; only sockets live here."""

import socket
import threading
from collections.abc import Callable, Iterator

import pytest

from edge import barrier

TIME_ALLOWED = 0.3
HOST = "hello.probe.invalid"
Script = Callable[[socket.socket, threading.Event], None]


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
