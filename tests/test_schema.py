#!/usr/bin/env python3
"""
Unit tests for validate_server_description, focused on Direct IP
Connection fields added in the 2026-04 Windrose patch.

Scope:
  1. Baseline valid doc (no Direct IP keys) still validates — the fields
     are optional on configs that predate the feature.
  2. Valid Direct IP config: UseDirectConnection=true + address set.
  3. UseDirectConnection=true without a server address is rejected
     (the footgun the release notes warn about — silent bounce to menu).
  4. Wrong types are rejected with shape errors.
  5. The stock -1 disabled sentinel is allowed only when Direct IP is off.
  6. Out-of-range port (0 or >65535) is rejected.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def _run(case: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


def _base_valid_doc() -> dict:
    """A minimal valid ServerDescription doc."""
    return {
        "Version": 1,
        "ServerDescription_Persistent": {
            "PersistentServerId": "1006E66345DA6416AA7A7E90A32630B4",
            "InviteCode": "c030f708",
            "IsPasswordProtected": False,
            "Password": "",
            "ServerName": "test",
            "WorldIslandId": "7A0A41E9616A4394A19B5F21A99C12B7",
            "MaxPlayerCount": 4,
            "P2pProxyAddress": "192.168.0.60",
        },
    }


def test_baseline_without_direct_ip_fields_validates():
    errs = server.validate_server_description(_base_valid_doc())
    assert errs == [], f"baseline should validate: {errs}"


def test_direct_ip_enabled_with_address_validates():
    doc = _base_valid_doc()
    doc["ServerDescription_Persistent"].update({
        "UseDirectConnection": True,
        "DirectConnectionServerAddress": "203.0.113.7",
        "DirectConnectionServerPort": 7777,
        "DirectConnectionProxyAddress": "0.0.0.0",
    })
    errs = server.validate_server_description(doc)
    assert errs == [], f"valid Direct IP config should pass: {errs}"


def test_direct_ip_enabled_without_address_rejected():
    """UseDirectConnection=true + empty address is the silent-bounce
    footgun called out in the release notes. Reject at validation time."""
    doc = _base_valid_doc()
    doc["ServerDescription_Persistent"].update({
        "UseDirectConnection": True,
        "DirectConnectionServerAddress": "",
        "DirectConnectionServerPort": 7777,
    })
    errs = server.validate_server_description(doc)
    assert any("DirectConnectionServerAddress" in e for e in errs), errs


def test_direct_ip_disabled_allows_empty_address():
    doc = _base_valid_doc()
    doc["ServerDescription_Persistent"].update({
        "UseDirectConnection": False,
        "DirectConnectionServerAddress": "",
        "DirectConnectionServerPort": 7777,
    })
    errs = server.validate_server_description(doc)
    assert errs == [], f"Direct IP off should allow empty address: {errs}"


def test_direct_ip_disabled_allows_minus_one_port_sentinel():
    doc = _base_valid_doc()
    doc["ServerDescription_Persistent"].update({
        "UseDirectConnection": False,
        "DirectConnectionServerAddress": "",
        "DirectConnectionServerPort": -1,
        "DirectConnectionProxyAddress": "0.0.0.0",
    })
    errs = server.validate_server_description(doc)
    assert errs == [], f"Direct IP off should allow -1 port sentinel: {errs}"


def test_direct_ip_enabled_rejects_minus_one_port_sentinel():
    doc = _base_valid_doc()
    doc["ServerDescription_Persistent"].update({
        "UseDirectConnection": True,
        "DirectConnectionServerAddress": "1.2.3.4",
        "DirectConnectionServerPort": -1,
    })
    errs = server.validate_server_description(doc)
    assert any("DirectConnectionServerPort" in e for e in errs), errs


def test_wrong_types_rejected():
    doc = _base_valid_doc()
    doc["ServerDescription_Persistent"]["UseDirectConnection"] = "yes"  # should be bool
    errs = server.validate_server_description(doc)
    assert any("UseDirectConnection" in e for e in errs), errs


def test_port_out_of_range_rejected():
    for bad_port in (0, 70000):
        doc = _base_valid_doc()
        doc["ServerDescription_Persistent"].update({
            "UseDirectConnection": True,
            "DirectConnectionServerAddress": "1.2.3.4",
            "DirectConnectionServerPort": bad_port,
        })
        errs = server.validate_server_description(doc)
        assert any("DirectConnectionServerPort" in e for e in errs), f"{bad_port}: {errs}"


if __name__ == "__main__":
    print("ServerDescription schema validation tests:")
    _run("baseline (no Direct IP keys)",              test_baseline_without_direct_ip_fields_validates)
    _run("Direct IP enabled + address — valid",       test_direct_ip_enabled_with_address_validates)
    _run("Direct IP enabled + no address — rejected", test_direct_ip_enabled_without_address_rejected)
    _run("Direct IP disabled + empty address OK",     test_direct_ip_disabled_allows_empty_address)
    _run("Direct IP disabled + -1 port OK",           test_direct_ip_disabled_allows_minus_one_port_sentinel)
    _run("Direct IP enabled + -1 port rejected",      test_direct_ip_enabled_rejects_minus_one_port_sentinel)
    _run("wrong types rejected",                      test_wrong_types_rejected)
    _run("port out of range rejected",                test_port_out_of_range_rejected)
    print("\nall schema validation tests passed")
