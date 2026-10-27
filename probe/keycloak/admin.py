"""The admin token of probe.py: master tokens live 60 s, and Keycloak rejects an expired one
before it acts, so a request that answers 401 is repeated once with a freshly minted token."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class Answer(Protocol):
    @property
    def status(self) -> int: ...


@dataclass
class AdminToken:
    """The mint is told how many tokens were made, so each one is registered under its own name."""

    mint: Callable[[int], str]
    minted: int = 0
    current: str = field(init=False)

    def __post_init__(self) -> None:
        self.renewed()

    def renewed(self) -> str:
        self.minted += 1
        self.current = self.mint(self.minted)
        return self.current

    def send[Response: Answer](self, request: Callable[[str], Response]) -> Response:
        response = request(self.current)
        if response.status != 401:
            return response
        return request(self.renewed())
