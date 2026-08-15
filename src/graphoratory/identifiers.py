from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from graphoratory.errors import IdentifierError


class ObjectType(StrEnum):
    WORKSPACE = "ws"
    LINE = "ln"
    GRAPH = "gr"
    CORPUS = "cp"


_FULL_HASH = re.compile(r"^[0-9a-f]{64}$")
_TYPED_ID = re.compile(r"^(ws|ln|gr|cp)-([0-9a-f]{8}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class Identifier:
    kind: ObjectType
    hash_full: str

    def __post_init__(self) -> None:
        if not _FULL_HASH.fullmatch(self.hash_full):
            raise IdentifierError("full hash must contain exactly 64 lowercase hex characters")

    @property
    def hash_short(self) -> str:
        return self.hash_full[:8]

    @property
    def display(self) -> str:
        return f"{self.kind.value}-{self.hash_short}"

    @classmethod
    def from_bytes(cls, kind: ObjectType, payload: bytes) -> Identifier:
        return cls(kind, hashlib.sha256(payload).hexdigest())


def parse_typed(value: str, expected: ObjectType | None = None) -> tuple[ObjectType, str]:
    match = _TYPED_ID.fullmatch(value)
    if match is None:
        raise IdentifierError(
            "identifier must use a lowercase typed prefix and 8 or 64 lowercase hex characters"
        )
    kind = ObjectType(match.group(1))
    if expected is not None and kind is not expected:
        raise IdentifierError(f"expected a {expected.value}- identifier, got {kind.value}-")
    return kind, match.group(2)


def resolve_typed(
    value: str,
    expected: ObjectType,
    candidates: Iterable[str],
) -> Identifier:
    _, hash_part = parse_typed(value, expected)
    matches = sorted({candidate for candidate in candidates if candidate.startswith(hash_part)})
    if not matches:
        raise IdentifierError(f"{value} does not resolve to an existing {expected.name.lower()}")
    if len(matches) > 1:
        raise IdentifierError(f"{value} is ambiguous; {len(matches)} objects match")
    return Identifier(expected, matches[0])
