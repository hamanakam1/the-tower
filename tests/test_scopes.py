"""Tests for hierarchical scope keys and access modes."""
import pytest

from the_tower.scopes import (
    ScopeClaim, ScopeMode, claims_conflict, expand_hierarchy, find_conflict,
    normalise, parse_claim, parse_claims,
)


def test_default_mode_is_write():
    c = parse_claim("data/train")
    assert c.mode == ScopeMode.WRITE
    assert c.path == "data/train"


def test_mode_suffix_is_parsed():
    assert parse_claim("data/train:read").mode == ScopeMode.READ
    assert parse_claim("data/train:write").mode == ScopeMode.WRITE
    assert parse_claim("data/train:intent").mode == ScopeMode.INTENT


def test_unknown_mode_falls_back_to_path():
    c = parse_claim("data/train:bogus")
    assert c.path == "data/train:bogus"


def test_two_readers_do_not_conflict():
    a = parse_claim("data/train:read")
    b = parse_claim("data/train:read")
    assert claims_conflict(a, b) is False


def test_reader_and_writer_conflict():
    a = parse_claim("data/train:read")
    b = parse_claim("data/train:write")
    assert claims_conflict(a, b) is True
    assert claims_conflict(b, a) is True


def test_two_writers_conflict():
    assert claims_conflict(parse_claim("d"), parse_claim("d")) is True


def test_intent_conflicts_with_everything():
    intent = parse_claim("d:intent")
    assert claims_conflict(intent, parse_claim("d:read")) is True
    assert claims_conflict(intent, parse_claim("d:write")) is True
    assert claims_conflict(intent, parse_claim("d:intent")) is True


def test_different_paths_do_not_conflict():
    assert claims_conflict(parse_claim("data/train"), parse_claim("data/test")) is False


def test_parent_covers_child():
    parent = parse_claim("model/lora")
    child = parse_claim("model/lora/v2")
    assert parent.covers(child) is True
    assert child.covers(parent) is False


def test_sibling_prefix_does_not_cover():
    a = parse_claim("model/lora")
    b = parse_claim("model/lora-adapters")
    assert a.covers(b) is False
    assert b.covers(a) is False


def test_parent_child_write_conflicts():
    assert claims_conflict(parse_claim("model/lora"), parse_claim("model/lora/v2")) is True


def test_parent_child_read_write_conflicts():
    assert claims_conflict(parse_claim("model/lora:read"),
                           parse_claim("model/lora/v2:write")) is True


def test_parent_child_two_reads_do_not_conflict():
    assert claims_conflict(parse_claim("model/lora:read"),
                           parse_claim("model/lora/v2:read")) is False


def test_find_conflict_returns_the_pair():
    a = parse_claims(["data/train:write"])
    b = parse_claims(["data/train:read"])
    pair = find_conflict(a, b)
    assert pair is not None
    assert pair[0].path == "data/train"


def test_find_conflict_returns_none_when_compatible():
    a = parse_claims(["data/train:read"])
    b = parse_claims(["data/train:read"])
    assert find_conflict(a, b) is None


def test_expand_hierarchy():
    assert expand_hierarchy(["model/lora/v2"]) == ["model", "model/lora", "model/lora/v2"]


def test_expand_hierarchy_of_several():
    out = expand_hierarchy(["a/b", "c"])
    assert out == ["a", "a/b", "c"]


def test_normalise_is_canonical():
    assert normalise(["b:read", "a"]) == ["a:write", "b:read"]


def test_empty_claim_is_rejected():
    with pytest.raises(ValueError):
        ScopeClaim(path="")


def test_claim_with_empty_segment_is_rejected():
    with pytest.raises(ValueError):
        ScopeClaim(path="a//b")


def test_claim_with_whitespace_segment_is_rejected():
    with pytest.raises(ValueError):
        ScopeClaim(path="a/ b")
