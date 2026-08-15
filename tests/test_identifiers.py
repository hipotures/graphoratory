import pytest

from graphoratory.errors import IdentifierError
from graphoratory.identifiers import Identifier, ObjectType, parse_typed, resolve_typed


def test_hash_and_typed_display_identifiers_are_lowercase() -> None:
    identifiers = {kind: Identifier.from_bytes(kind, kind.value.encode()) for kind in ObjectType}
    assert all(len(identifier.digest) == 64 for identifier in identifiers.values())
    assert identifiers[ObjectType.WORKSPACE].display.startswith("ws-")
    assert identifiers[ObjectType.LINE].display.startswith("ln-")
    assert identifiers[ObjectType.GRAPH].display.startswith("gr-")
    assert all(
        identifier.display == identifier.display.lower() for identifier in identifiers.values()
    )


def test_uppercase_typed_identifier_is_rejected() -> None:
    with pytest.raises(IdentifierError, match="lowercase"):
        parse_typed("".join(("W", "S", "-a1b2c3d4")))


def test_short_and_full_typed_identifier_resolution() -> None:
    full_hash = "a1b2c3d4" + "0" * 56
    assert resolve_typed("ws-a1b2c3d4", ObjectType.WORKSPACE, [full_hash]).digest == full_hash
    assert resolve_typed(f"ws-{full_hash}", ObjectType.WORKSPACE, [full_hash]).digest == full_hash


def test_ambiguous_short_identifier_fails() -> None:
    candidates = ["a1b2c3d4" + "0" * 56, "a1b2c3d4" + "1" * 56]
    with pytest.raises(IdentifierError, match="ambiguous"):
        resolve_typed("ws-a1b2c3d4", ObjectType.WORKSPACE, candidates)
