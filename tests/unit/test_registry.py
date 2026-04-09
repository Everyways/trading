"""Tests for the generic plugin registry (§17.2 — plugin registry test)."""

import pytest

from app.core.registry import Registry


class _Base:
    pass


def _make_registry() -> Registry[_Base]:
    """Create a fresh registry for each test to avoid cross-test pollution."""
    return Registry("test")


def test_register_and_retrieve() -> None:
    registry = _make_registry()

    @registry.register("foo")
    class Foo(_Base):
        pass

    assert registry.get("foo") is Foo


def test_registered_class_is_returned_exactly() -> None:
    registry = _make_registry()

    @registry.register("bar")
    class Bar(_Base):
        x = 42

    cls = registry.get("bar")
    assert cls.x == 42  # type: ignore[attr-defined]


def test_duplicate_key_raises_value_error() -> None:
    registry = _make_registry()

    @registry.register("dup")
    class First(_Base):
        pass

    with pytest.raises(ValueError, match="already registered"):

        @registry.register("dup")
        class Second(_Base):
            pass


def test_unknown_key_raises_key_error() -> None:
    registry = _make_registry()

    with pytest.raises(KeyError, match="not found"):
        registry.get("ghost")


def test_all_returns_copy_of_items() -> None:
    registry = _make_registry()

    @registry.register("a")
    class A(_Base):
        pass

    @registry.register("b")
    class B(_Base):
        pass

    items = registry.all()
    assert "a" in items
    assert "b" in items
    assert len(items) == 2


def test_all_returns_independent_copy() -> None:
    """Mutating the returned dict must not affect the registry."""
    registry = _make_registry()

    @registry.register("x")
    class X(_Base):
        pass

    items = registry.all()
    del items["x"]
    assert "x" in registry  # registry unchanged


def test_len() -> None:
    registry = _make_registry()

    @registry.register("p")
    class P(_Base):
        pass

    assert len(registry) == 1


def test_contains() -> None:
    registry = _make_registry()

    @registry.register("q")
    class Q(_Base):
        pass

    assert "q" in registry
    assert "z" not in registry


def test_registry_name_in_error_message() -> None:
    registry: Registry[_Base] = Registry("broker")

    with pytest.raises(KeyError, match="broker"):
        registry.get("missing")


def test_multiple_registries_are_independent() -> None:
    """Two Registry instances must not share state."""
    r1: Registry[_Base] = Registry("r1")
    r2: Registry[_Base] = Registry("r2")

    @r1.register("shared_key")
    class C1(_Base):
        pass

    # r2 should not be affected by r1's registration
    assert "shared_key" not in r2
