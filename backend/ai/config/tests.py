"""
Internal tests for the AI Configuration Layer.

Run directly::

    python -m backend.ai.config.tests

Deterministic, offline, no network, no database.
"""
from __future__ import annotations

import sys
from typing import List, Tuple

from backend.ai.config import AIConfig, ConfigManager, ConfigSnapshot, ConfigValidationError


def _check(name: str, condition: bool, detail: str = "") -> bool:
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + detail) if detail else ''}")
    return condition


def test_defaults() -> bool:
    print("test_defaults")
    mgr = ConfigManager()
    ok = True
    ok &= _check("enabled defaults False", mgr.get("enabled") is False)
    ok &= _check("provider defaults dummy", mgr.get("provider") == "dummy")
    ok &= _check("temperature defaults 1.0", mgr.get("temperature") == 1.0)
    ok &= _check("top_p defaults 1.0", mgr.get("top_p") == 1.0)
    ok &= _check("max_tokens defaults 4096", mgr.get("max_tokens") == 4096)
    ok &= _check("timeout defaults 30", mgr.get("timeout") == 30)
    ok &= _check("retry_count defaults 3", mgr.get("retry_count") == 3)
    ok &= _check("history_budget defaults 4000", mgr.get("history_budget") == 4000)
    ok &= _check("tool_budget defaults 2000", mgr.get("tool_budget") == 2000)
    ok &= _check("streaming defaults False", mgr.get("streaming_enabled") is False)
    ok &= _check("vision defaults False", mgr.get("vision_enabled") is False)
    ok &= _check("reasoning defaults False", mgr.get("reasoning_enabled") is False)
    ok &= _check("developer_mode defaults False", mgr.get("developer_mode") is False)
    return ok


def test_set_valid() -> bool:
    print("test_set_valid")
    mgr = ConfigManager()
    mgr.set("temperature", 0.5)
    mgr.set("top_p", 0.8)
    mgr.set("max_tokens", 2048)
    mgr.set("timeout", 60)
    mgr.set("enabled", True)
    ok = True
    ok &= _check("temperature set", mgr.get("temperature") == 0.5)
    ok &= _check("top_p set", mgr.get("top_p") == 0.8)
    ok &= _check("max_tokens set", mgr.get("max_tokens") == 2048)
    ok &= _check("timeout set", mgr.get("timeout") == 60)
    ok &= _check("enabled set", mgr.get("enabled") is True)
    return ok


def test_set_invalid_temperature() -> bool:
    print("test_set_invalid_temperature")
    mgr = ConfigManager()
    ok = True
    try:
        mgr.set("temperature", 3.0)
        ok &= _check("temperature > 2.0 rejected", False)
    except ConfigValidationError:
        ok &= _check("temperature > 2.0 rejected", True)
    try:
        mgr.set("temperature", -0.1)
        ok &= _check("temperature < 0.0 rejected", False)
    except ConfigValidationError:
        ok &= _check("temperature < 0.0 rejected", True)
    return ok


def test_set_invalid_top_p() -> bool:
    print("test_set_invalid_top_p")
    mgr = ConfigManager()
    ok = True
    try:
        mgr.set("top_p", 1.5)
        ok &= _check("top_p > 1.0 rejected", False)
    except ConfigValidationError:
        ok &= _check("top_p > 1.0 rejected", True)
    try:
        mgr.set("top_p", -0.1)
        ok &= _check("top_p < 0.0 rejected", False)
    except ConfigValidationError:
        ok &= _check("top_p < 0.0 rejected", True)
    return ok


def test_set_invalid_max_tokens() -> bool:
    print("test_set_invalid_max_tokens")
    mgr = ConfigManager()
    ok = True
    try:
        mgr.set("max_tokens", 0)
        ok &= _check("max_tokens=0 rejected", False)
    except ConfigValidationError:
        ok &= _check("max_tokens=0 rejected", True)
    try:
        mgr.set("max_tokens", -10)
        ok &= _check("max_tokens<0 rejected", False)
    except ConfigValidationError:
        ok &= _check("max_tokens<0 rejected", True)
    return ok


def test_set_invalid_timeout() -> bool:
    print("test_set_invalid_timeout")
    mgr = ConfigManager()
    ok = True
    try:
        mgr.set("timeout", 0)
        ok &= _check("timeout=0 rejected", False)
    except ConfigValidationError:
        ok &= _check("timeout=0 rejected", True)
    return ok


def test_set_invalid_type() -> bool:
    print("test_set_invalid_type")
    mgr = ConfigManager()
    ok = True
    try:
        mgr.set("enabled", "yes")
        ok &= _check("enabled='yes' rejected", False)
    except ConfigValidationError:
        ok &= _check("enabled='yes' rejected", True)
    try:
        mgr.set("temperature", "hot")
        ok &= _check("temperature='hot' rejected", False)
    except ConfigValidationError:
        ok &= _check("temperature='hot' rejected", True)
    return ok


def test_snapshot_immutable() -> bool:
    print("test_snapshot_immutable")
    mgr = ConfigManager()
    mgr.set("temperature", 0.7)
    snap = mgr.snapshot()
    ok = True
    ok &= _check("snapshot is ConfigSnapshot", isinstance(snap, ConfigSnapshot))
    ok &= _check("snapshot has temperature", snap.temperature == 0.7)
    try:
        snap.temperature = 1.0  # type: ignore[misc]
        ok &= _check("snapshot is frozen", False)
    except Exception:
        ok &= _check("snapshot is frozen", True)
    return ok


def test_snapshot_independent_of_config() -> bool:
    print("test_snapshot_independent_of_config")
    mgr = ConfigManager()
    mgr.set("temperature", 0.5)
    snap = mgr.snapshot()
    mgr.set("temperature", 1.5)
    ok = True
    ok &= _check("snapshot unaffected by later set", snap.temperature == 0.5)
    return ok


def test_reset() -> bool:
    print("test_reset")
    mgr = ConfigManager()
    mgr.set("temperature", 0.3)
    mgr.set("enabled", True)
    mgr.reset()
    ok = True
    ok &= _check("temperature reset to 1.0", mgr.get("temperature") == 1.0)
    ok &= _check("enabled reset to False", mgr.get("enabled") is False)
    return ok


def test_clone() -> bool:
    print("test_clone")
    mgr = ConfigManager()
    mgr.set("temperature", 0.4)
    cloned = mgr.clone()
    cloned.set("temperature", 0.9)
    ok = True
    ok &= _check("original unaffected by clone change", mgr.get("temperature") == 0.4)
    ok &= _check("clone has its own value", cloned.get("temperature") == 0.9)
    return ok


def test_validate() -> bool:
    print("test_validate")
    mgr = ConfigManager()
    errors = mgr.validate()
    ok = _check("default config is valid", len(errors) == 0, str(errors))
    return ok


def test_provider_overrides() -> bool:
    print("test_provider_overrides")
    mgr = ConfigManager()
    mgr.register_provider_overrides("gemini", {"temperature": 0.9, "timeout": 45})
    mgr.set("provider", "gemini")
    snap = mgr.snapshot()
    ok = True
    ok &= _check("override temperature applied", snap.temperature == 0.9)
    ok &= _check("override timeout applied", snap.timeout == 45)
    ok &= _check("base config unchanged", mgr.get("temperature") == 1.0)
    mgr.set("provider", "dummy")
    snap2 = mgr.snapshot()
    ok &= _check("no override for dummy", snap2.temperature == 1.0)
    return ok


def test_set_many_atomic() -> bool:
    print("test_set_many_atomic")
    mgr = ConfigManager()
    ok = True
    try:
        mgr.set_many({"temperature": 0.5, "top_p": 2.0})
        ok &= _check("set_many rejected invalid batch", False)
    except ConfigValidationError:
        ok &= _check("set_many rejected invalid batch", True)
    ok &= _check("temperature unchanged after failed batch", mgr.get("temperature") == 1.0)
    mgr.set_many({"temperature": 0.5, "top_p": 0.8})
    ok &= _check("set_many applied valid batch", mgr.get("temperature") == 0.5 and mgr.get("top_p") == 0.8)
    return ok


def run_all() -> int:
    tests: List[Tuple[str, callable]] = [
        ("defaults", test_defaults),
        ("set_valid", test_set_valid),
        ("invalid_temperature", test_set_invalid_temperature),
        ("invalid_top_p", test_set_invalid_top_p),
        ("invalid_max_tokens", test_set_invalid_max_tokens),
        ("invalid_timeout", test_set_invalid_timeout),
        ("invalid_type", test_set_invalid_type),
        ("snapshot_immutable", test_snapshot_immutable),
        ("snapshot_independent", test_snapshot_independent_of_config),
        ("reset", test_reset),
        ("clone", test_clone),
        ("validate", test_validate),
        ("provider_overrides", test_provider_overrides),
        ("set_many_atomic", test_set_many_atomic),
    ]
    failures = 0
    for name, fn in tests:
        print(f"== {name} ==")
        try:
            if not fn():
                failures += 1
                print(f"  !! {name} reported failures")
        except Exception as exc:
            failures += 1
            print(f"  !! {name} raised: {exc!r}")
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} TEST(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_all())
