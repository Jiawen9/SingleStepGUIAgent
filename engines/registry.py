"""Engine registration and configured ordering."""

from __future__ import annotations

from .base import Engine


def order_engines(
    engines: tuple[Engine, ...], order: tuple[str, ...]
) -> tuple[Engine, ...]:
    by_name = {engine.name: engine for engine in engines}
    unknown = [name for name in order if name not in by_name]
    if unknown:
        raise ValueError("Unknown engines: " + ", ".join(unknown))
    ordered = [by_name.pop(name) for name in order]
    ordered.extend(sorted(by_name.values(), key=lambda item: item.priority))
    return tuple(ordered)
