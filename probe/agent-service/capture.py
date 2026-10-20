#!/usr/bin/env python3
"""A bounded packet window for the agent-service probe (#189): standard library only, run in a
container with NET_RAW and nothing else, in the namespace it is asked to watch.

    capture.py syn                     TCP SYNs that leave this namespace
    capture.py dns LABEL [LABEL ...]   DNS questions on port 53, UDP or TCP, naming a label

It opens an `AF_PACKET` socket on every interface, prints `ready`, watches for at most
WINDOW_SECONDS or FRAME_CAP frames, whichever comes first, and prints one JSON line: the counts,
the addresses and ports named below, and the kernel's own PACKET_STATISTICS for the socket. Every
frame is parsed in memory and dropped. No packet file, and no payload in any output or diagnostic:
the host's window also sees the operator's own traffic. The caller decides whether the window was
complete (no drop, still open when its control ended).

The `dns` window attaches a kernel filter that passes only frames with port 53 (and every frame of
an interface without an Ethernet header), so a busy host does not fill the frame cap with traffic
the window does not look at. The filter reads IPv4 and IPv6 headers without extensions, first
fragments only; a DNS question behind IPv6 extension headers is not seen.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass

WINDOW_SECONDS = 20
FRAME_CAP = 20_000
RECEIVE_BUFFER_BYTES = 16 * 1024 * 1024
READ_BYTES = 1600  # headers and a DNS question fit; the rest of a frame is never kept
POLL_SECONDS = 0.5
ETH_P_ALL = 3
SOL_PACKET = 263
PACKET_STATISTICS = 6
PACKET_OUTGOING = 4
ARPHRD_ETHER = 1
ARPHRD_LOOPBACK = 772  # carries an Ethernet header too
ETHERNET_HEADER_BYTES = 14
VLAN_TAG_BYTES = 4
ETHERTYPE_VLAN = 0x8100
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD
IPV6_HEADER_BYTES = 40
UDP_HEADER_BYTES = 8
TCP_SYN, TCP_ACK = 0x02, 0x10
PROTOCOL_TCP, PROTOCOL_UDP = 6, 17
DNS_PORT = 53
DNS_HEADER_BYTES = 12
LABEL = re.compile(r"[a-z0-9]{1,63}")

# Classic BPF: opcodes, and the ancillary load of the interface's hardware type (SKF_AD_OFF + 28).
SO_ATTACH_FILTER = 26
LOAD_WORD, LOAD_HALF, LOAD_BYTE = 0x20, 0x28, 0x30  # absolute offset
LOAD_HALF_INDEXED, LOAD_HEADER_LENGTH_INTO_X = 0x48, 0xB1
JUMP_EQUAL, JUMP_ANY_BIT, RETURN = 0x15, 0x45, 0x06
HARDWARE_TYPE_OFFSET = 0xFFFFF01C
PASS_FRAME, DROP_FRAME = 0xFFFF, 0
ETHERTYPE_OFFSET, IPV4_PROTOCOL_OFFSET, IPV4_FRAGMENT_OFFSET, IPV4_HEADER_OFFSET = 12, 23, 20, 14
IPV6_PROTOCOL_OFFSET, IPV6_SOURCE_PORT_OFFSET = 20, 54
FRAGMENT_BITS = 0x1FFF


@dataclass(frozen=True)
class Segment:
    """What the window keeps of one TCP or UDP frame: no payload but the DNS question."""

    outgoing: bool
    interface: str
    protocol: int
    destination: str
    source_port: int
    destination_port: int
    flags: int
    question: str


def link_header_bytes(frame: bytes, hardware_type: int) -> tuple[int, int | None]:
    """The link header's length and the IP version it announces (None: read it from the packet)."""
    if hardware_type not in (ARPHRD_ETHER, ARPHRD_LOOPBACK):
        return 0, None
    length, ethertype = ETHERNET_HEADER_BYTES, struct.unpack_from("!H", frame, 12)[0]
    if ethertype == ETHERTYPE_VLAN:
        length, ethertype = length + VLAN_TAG_BYTES, struct.unpack_from("!H", frame, 16)[0]
    return length, {ETHERTYPE_IPV4: 4, ETHERTYPE_IPV6: 6}.get(ethertype)


def parse(frame: bytes, interface: str, packet_type: int, hardware_type: int) -> Segment | None:
    offset, version = link_header_bytes(frame, hardware_type)
    if len(frame) <= offset:
        return None
    version = version or frame[offset] >> 4
    if version == 4:
        header_bytes = (frame[offset] & 0x0F) * 4
        protocol = frame[offset + 9]
        destination = socket.inet_ntop(socket.AF_INET, frame[offset + 16 : offset + 20])
    elif version == 6:
        header_bytes = IPV6_HEADER_BYTES
        protocol = frame[offset + 6]
        destination = socket.inet_ntop(socket.AF_INET6, frame[offset + 24 : offset + 40])
    else:
        return None
    transport = frame[offset + header_bytes :]
    if protocol not in (PROTOCOL_TCP, PROTOCOL_UDP) or len(transport) < UDP_HEADER_BYTES:
        return None
    source_port, destination_port = struct.unpack_from("!HH", transport)
    flags, payload = 0, transport[UDP_HEADER_BYTES:]
    if protocol == PROTOCOL_TCP:
        if len(transport) < 20:
            return None
        flags, payload = transport[13], transport[(transport[12] >> 4) * 4 :]
        payload = payload[2:]  # DNS over TCP: the two-byte length comes first
    question = dns_question(payload) if DNS_PORT in (source_port, destination_port) else ""
    return Segment(
        packet_type == PACKET_OUTGOING, interface, protocol, destination,
        source_port, destination_port, flags, question,
    )  # fmt: skip


def dns_question(message: bytes) -> str:
    """The first question's name, lower-cased and dotted; empty for anything else."""
    labels, at = [], DNS_HEADER_BYTES
    while at < len(message) and message[at]:
        length = message[at]
        labels.append(message[at + 1 : at + 1 + length].decode("ascii", errors="replace"))
        at += 1 + length
    return ".".join(labels).lower()


# (label, opcode, operand, jump to this label if true, if false); no label: the next instruction.
Step = tuple[str | None, int, int, str | None, str | None]
DNS_FILTER: list[Step] = [
    (None, LOAD_WORD, HARDWARE_TYPE_OFFSET, None, None),
    (None, JUMP_EQUAL, ARPHRD_ETHER, "ethernet", None),
    (None, JUMP_EQUAL, ARPHRD_LOOPBACK, "ethernet", "pass"),
    ("ethernet", LOAD_HALF, ETHERTYPE_OFFSET, None, None),
    (None, JUMP_EQUAL, ETHERTYPE_IPV4, "ipv4", None),
    (None, JUMP_EQUAL, ETHERTYPE_IPV6, "ipv6", None),
    (None, JUMP_EQUAL, ETHERTYPE_VLAN, "pass", "drop"),
    ("ipv4", LOAD_BYTE, IPV4_PROTOCOL_OFFSET, None, None),
    (None, JUMP_EQUAL, PROTOCOL_TCP, "ipv4 ports", None),
    (None, JUMP_EQUAL, PROTOCOL_UDP, "ipv4 ports", "drop"),
    ("ipv4 ports", LOAD_HALF, IPV4_FRAGMENT_OFFSET, None, None),
    (None, JUMP_ANY_BIT, FRAGMENT_BITS, "drop", None),
    (None, LOAD_HEADER_LENGTH_INTO_X, IPV4_HEADER_OFFSET, None, None),
    (None, LOAD_HALF_INDEXED, IPV4_HEADER_OFFSET, None, None),
    (None, JUMP_EQUAL, DNS_PORT, "pass", None),
    (None, LOAD_HALF_INDEXED, IPV4_HEADER_OFFSET + 2, None, None),
    (None, JUMP_EQUAL, DNS_PORT, "pass", "drop"),
    ("ipv6", LOAD_BYTE, IPV6_PROTOCOL_OFFSET, None, None),
    (None, JUMP_EQUAL, PROTOCOL_TCP, "ipv6 ports", None),
    (None, JUMP_EQUAL, PROTOCOL_UDP, "ipv6 ports", "drop"),
    ("ipv6 ports", LOAD_HALF, IPV6_SOURCE_PORT_OFFSET, None, None),
    (None, JUMP_EQUAL, DNS_PORT, "pass", None),
    (None, LOAD_HALF, IPV6_SOURCE_PORT_OFFSET + 2, None, None),
    (None, JUMP_EQUAL, DNS_PORT, "pass", "drop"),
    ("pass", RETURN, PASS_FRAME, None, None),
    ("drop", RETURN, DROP_FRAME, None, None),
]


def attach_filter(watcher: socket.socket, steps: list[Step]) -> None:
    """The kernel's classic-BPF filter for the socket, from steps whose jumps name labels."""
    at = {label: index for index, (label, *_) in enumerate(steps) if label}
    program = b"".join(
        struct.pack(
            "HBBI",
            opcode,
            at[yes] - index - 1 if yes else 0,
            at[no] - index - 1 if no else 0,
            operand,
        )
        for index, (_, opcode, operand, yes, no) in enumerate(steps)
    )
    instructions = ctypes.create_string_buffer(program, len(program))
    descriptor = struct.pack("@HL", len(steps), ctypes.addressof(instructions))
    watcher.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, descriptor)


class Window:
    def __init__(self) -> None:
        self.frames = 0
        self.syns: list[dict] = []
        self.label_frames: dict[str, int] = {}

    def see(self, segment: Segment, labels: list[str]) -> None:
        opens_a_connection = segment.protocol == PROTOCOL_TCP and (
            segment.flags & (TCP_SYN | TCP_ACK) == TCP_SYN
        )
        if segment.outgoing and opens_a_connection:
            self.syns.append(
                {
                    "at": time.time(),
                    "interface": segment.interface,
                    "destination": segment.destination,
                    "port": segment.destination_port,
                }
            )
        for label in labels:
            if label in segment.question:
                self.label_frames[label] = self.label_frames.get(label, 0) + 1


def open_socket(steps: list[Step] | None) -> socket.socket:
    watcher = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    watcher.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES)
    if steps:
        attach_filter(watcher, steps)
        # What queued before the filter was attached is not the window's.
        watcher.setblocking(False)
        with contextlib.suppress(BlockingIOError):
            while True:
                watcher.recv(READ_BYTES)
        read_statistics(watcher)
    watcher.settimeout(POLL_SECONDS)
    return watcher


def read_statistics(watcher: socket.socket) -> tuple[int, int]:
    """The kernel's counts for the socket since the last read: (packets seen, packets dropped)."""
    return struct.unpack("II", watcher.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))


def watch(watcher: socket.socket, labels: list[str]) -> tuple[Window, str]:
    window, deadline = Window(), time.monotonic() + WINDOW_SECONDS
    while window.frames < FRAME_CAP:
        if time.monotonic() >= deadline:
            return window, "seconds"
        try:
            frame, (interface, _, packet_type, hardware_type, _) = watcher.recvfrom(READ_BYTES)
        except TimeoutError:
            continue
        window.frames += 1
        if segment := parse(frame, interface, packet_type, hardware_type):
            window.see(segment, labels)
    return window, "frames"


def run(mode: str, labels: list[str]) -> dict:
    watcher = open_socket(DNS_FILTER if mode == "dns" else None)
    print("ready", flush=True)
    started = time.time()
    window, ended_by = watch(watcher, labels)
    packets, drops = read_statistics(watcher)
    result = {
        "started_at": started,
        "ended_at": time.time(),
        "ended_by": ended_by,
        "frames": window.frames,
        "packets": packets,
        "drops": drops,
    }
    return result | ({"syns": window.syns} if mode == "syn" else {"labels": window.label_frames})


def main(arguments: list[str]) -> int:
    mode, labels = (arguments[0] if arguments else ""), [label.lower() for label in arguments[1:]]
    valid = (mode == "syn" and not labels) or (
        mode == "dns" and labels and all(LABEL.fullmatch(label) for label in labels)
    )
    if not valid:
        print("usage: capture.py syn | capture.py dns LABEL [LABEL ...]", file=sys.stderr)
        return 2
    print(json.dumps(run(mode, labels)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
