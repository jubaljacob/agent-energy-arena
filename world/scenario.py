"""Scenario protocol + loader (open-source-arena slice 02).

Every shipped and user-authored stress scenario obeys a one-method contract:

    class MyScenario(Scenario):
        seed = 42

        def apply(self, world, day):
            ...

`apply` is called once per simulated day, after the expiry pass over
finite-duration events and before the stochastic event sampler
(`world.sim.World._advance_one_day`). A scenario writes into
`state.weather_overrides` for transient per-hour clips, mutates mutable
fields on world state for policy/market shocks, or appends event dicts
to `state.active_events` for forced event injections.

`NullScenario` is the default attached to every fresh `World`: its
`apply` is a no-op, so the byte trace of a baseline-seed run is
unchanged by introducing the scenario hook.

`load_scenario(dotted_path)` imports a module by dotted path and walks
its top-level attributes for a concrete `Scenario` subclass to
instantiate. The discovery mirrors the agent loader in `evaluate.py`.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from world.sim import World


# Mirrors the agent-folder walk in `world/api.py`: hidden dirs are
# skipped via the leading-dot check at the walk site; only `__pycache__`
# needs an explicit name skip. Kept narrow on purpose — broadening the
# skip set silently hides scenarios from the picker.
_SCENARIO_WALK_SKIP: frozenset[str] = frozenset({"__pycache__"})


class Scenario:
    """Base class for stress scenarios.

    Subclasses override `apply(world, day)` to inject overrides, mutate
    state fields, or append active events on the given day. The base
    implementation is a no-op so a scenario can be subclassed without
    re-declaring an empty apply.
    """

    # Default replay seed. Subclasses override at the class level so the
    # arena runner can read `cls.seed` without instantiating.
    seed: int = 42

    def apply(self, world: World, day: int) -> None:
        return None


class NullScenario(Scenario):
    """Default scenario: does nothing. Attached to every fresh world."""

    pass


def discover_scenarios(scenarios_root: Path) -> list[str]:
    """Walk `scenarios_root` for `.py` modules that define a concrete
    `Scenario` subclass and return their importable dotted paths.

    Powers `GET /scenarios`, the symmetric counterpart of
    `GET /agent/folders` that backs the UI's scenario picker. Dotted
    paths are built relative to `scenarios_root.parent`, so a file at
    `repo/scenarios/baseline.py` resolves to `scenarios.baseline`.

    Skip rules: hidden dirs (leading `.`) and `__pycache__`. `__init__.py`
    is skipped — importing it as `<pkg>.__init__` would never round-trip
    through `load_scenario`. A module that raises on import is silently
    skipped so one broken file cannot break the picker for the rest.

    The walk filters on the same Scenario-subclass rule as `load_scenario`
    (excludes `Scenario` and `NullScenario` themselves). The returned list
    is sorted alphabetically for stable UI rendering.
    """
    root = scenarios_root.resolve()
    parent = root.parent
    found: list[str] = []

    def walk(current: Path) -> None:
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                if entry.name.startswith(".") or entry.name in _SCENARIO_WALK_SKIP:
                    continue
                walk(entry)
                continue
            if not entry.is_file() or entry.suffix != ".py" or entry.name == "__init__.py":
                continue
            rel = entry.relative_to(parent).with_suffix("")
            dotted = ".".join(rel.parts)
            try:
                mod = importlib.import_module(dotted)
            except Exception:
                continue
            for value in vars(mod).values():
                if (
                    isinstance(value, type)
                    and issubclass(value, Scenario)
                    and value is not Scenario
                    and value is not NullScenario
                ):
                    found.append(dotted)
                    break

    walk(root)
    found.sort()
    return found


def load_scenario(dotted_path: str) -> Scenario:
    """Resolve a scenario subclass by dotted module path and return an
    instance.

    Mirrors the agent loader in `evaluate.py`: imports the module, then
    walks its top-level attributes for a concrete `Scenario` subclass
    (excluding `Scenario` and `NullScenario` themselves). Raises
    `ImportError` if the module cannot be imported, `ValueError` if no
    Scenario subclass is found.
    """
    try:
        mod = importlib.import_module(dotted_path)
    except ImportError as exc:
        raise ImportError(f"could not import scenario module {dotted_path!r}: {exc}") from exc

    for value in vars(mod).values():
        if (
            isinstance(value, type)
            and issubclass(value, Scenario)
            and value is not Scenario
            and value is not NullScenario
        ):
            return value()

    raise ValueError(f"module {dotted_path!r} does not define a Scenario subclass")
