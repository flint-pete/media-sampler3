# Stage 3 tests for node identity resolution (nodemeta.py).
#
# PURE tests: no node, no network. Verify manifest parsing, precedence
# (explicit > manifest > /etc/waggle files), and graceful absence.

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import nodemeta  # noqa: E402


_INJECTED_ENV = ("WAGGLE_NODE_VSN", "WAGGLE_NODE_ID", "WAGGLE_NODE_GPS_LAT",
                 "WAGGLE_NODE_GPS_LON", "WAGGLE_NODE_MOBILITY")


@pytest.fixture(autouse=True)
def _clean_injected_env(monkeypatch):
    # Isolate every test from any ambient WES-injected identity env (e.g. when the
    # suite runs inside a plugin pod). Tests that want it set it explicitly.
    for k in _INJECTED_ENV:
        monkeypatch.delenv(k, raising=False)


MANIFEST = {
    "vsn": "H00F",
    "name": "00004CBB4701D16C",
    "gps_lat": 41.7179852752395,
    "gps_lon": -87.98271513806043,
    "project": "SGT",
}


def write_manifest(tmp_path, obj):
    p = tmp_path / "node-manifest-v2.json"
    p.write_text(json.dumps(obj))
    return str(p)


def test_load_manifest_ok(tmp_path):
    path = write_manifest(tmp_path, MANIFEST)
    m = nodemeta.load_manifest(path)
    assert m["vsn"] == "H00F"
    assert m["gps_lat"] == 41.7179852752395


def test_load_manifest_missing_returns_empty(tmp_path):
    assert nodemeta.load_manifest(str(tmp_path / "nope.json")) == {}


def test_load_manifest_malformed_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert nodemeta.load_manifest(str(p)) == {}


def test_resolve_from_manifest(tmp_path):
    path = write_manifest(tmp_path, MANIFEST)
    ident = nodemeta.resolve_identity(manifest_path=path)
    assert ident["vsn"] == "H00F"
    assert ident["node_id"] == "00004CBB4701D16C"
    assert abs(ident["lat"] - 41.7179852752395) < 1e-12
    assert abs(ident["lon"] - (-87.98271513806043)) < 1e-12


def test_explicit_overrides_manifest(tmp_path):
    path = write_manifest(tmp_path, MANIFEST)
    ident = nodemeta.resolve_identity(
        vsn="W123", lat=1.5, lon=2.5, manifest_path=path)
    assert ident["vsn"] == "W123"      # explicit wins
    assert ident["lat"] == 1.5
    assert ident["lon"] == 2.5
    assert ident["node_id"] == "00004CBB4701D16C"  # not overridden -> manifest


def test_resolve_no_sources_uses_placeholder(tmp_path, monkeypatch):
    # No explicit args, no runtime lookup (placeholder returns None), no manifest,
    # no /etc/waggle files -> vsn falls back to PLACEHOLDER_VSN, gps omitted.
    monkeypatch.setattr(nodemeta, "DEFAULT_VSN_FILE", str(tmp_path / "no-vsn"))
    monkeypatch.setattr(nodemeta, "DEFAULT_NODE_ID_FILE", str(tmp_path / "no-id"))
    monkeypatch.setattr(nodemeta, "PLACEHOLDER_VSN", "NODE")
    ident = nodemeta.resolve_identity(manifest_path=str(tmp_path / "no-manifest.json"))
    assert ident["vsn"] == "NODE"
    assert ident["vsn_is_placeholder"] is True
    assert ident["node_id"] is None
    assert ident["lat"] is None and ident["lon"] is None  # never faked


def test_resolved_vsn_not_flagged_placeholder(tmp_path):
    # A real resolved vsn (from manifest) must NOT be flagged as placeholder.
    path = write_manifest(tmp_path, MANIFEST)
    ident = nodemeta.resolve_identity(manifest_path=path)
    assert ident["vsn"] == "H00F"
    assert ident["vsn_is_placeholder"] is False


def test_explicit_vsn_not_flagged_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(nodemeta, "DEFAULT_VSN_FILE", str(tmp_path / "no-vsn"))
    ident = nodemeta.resolve_identity(
        vsn="W999", manifest_path=str(tmp_path / "no-manifest.json"))
    assert ident["vsn"] == "W999"
    assert ident["vsn_is_placeholder"] is False


def test_runtime_identity_reads_injected_env(monkeypatch):
    # The WES wes-nodeinfo-injection change projects these 5 env vars into the pod.
    # _runtime_identity() reads them with sentinel->None normalization.
    monkeypatch.setenv("WAGGLE_NODE_VSN", "H00F")
    monkeypatch.setenv("WAGGLE_NODE_ID", "00004cbb4701d16c")
    monkeypatch.setenv("WAGGLE_NODE_GPS_LAT", "41.7179852752395")
    monkeypatch.setenv("WAGGLE_NODE_GPS_LON", "-87.98271513806043")
    rt = nodemeta._runtime_identity()
    assert rt["vsn"] == "H00F"
    assert rt["node_id"] == "00004cbb4701d16c"
    assert abs(rt["lat"] - 41.7179852752395) < 1e-12
    assert abs(rt["lon"] - (-87.98271513806043)) < 1e-12


@pytest.mark.parametrize("env,expect", [
    # sentinel contract: mirrors pywaggle2 node_info_env.py
    ({"WAGGLE_NODE_VSN": "0"}, {"vsn": None}),          # VSN sentinel
    ({"WAGGLE_NODE_VSN": ""}, {"vsn": None}),
    ({"WAGGLE_NODE_ID": ""}, {"node_id": None}),
    ({"WAGGLE_NODE_GPS_LAT": "999"}, {"lat": None}),    # off-globe sentinel by range
    ({"WAGGLE_NODE_GPS_LON": "999"}, {"lon": None}),
    ({"WAGGLE_NODE_GPS_LAT": "notafloat"}, {"lat": None}),
    ({"WAGGLE_NODE_GPS_LAT": "91"}, {"lat": None}),     # out of range
    ({"WAGGLE_NODE_GPS_LON": "181"}, {"lon": None}),
])
def test_runtime_identity_sentinels_to_none(monkeypatch, env, expect):
    for k in ("WAGGLE_NODE_VSN", "WAGGLE_NODE_ID",
              "WAGGLE_NODE_GPS_LAT", "WAGGLE_NODE_GPS_LON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rt = nodemeta._runtime_identity()
    for k, want in expect.items():
        assert rt[k] == want


def test_runtime_identity_empty_env_is_inert(monkeypatch):
    # Off-node / no injection -> all None, so manifest/file fallbacks still drive
    # resolution (precedence tests below rely on this).
    for k in ("WAGGLE_NODE_VSN", "WAGGLE_NODE_ID",
              "WAGGLE_NODE_GPS_LAT", "WAGGLE_NODE_GPS_LON", "WAGGLE_NODE_MOBILITY"):
        monkeypatch.delenv(k, raising=False)
    assert nodemeta._runtime_identity() == {
        "vsn": None, "node_id": None, "lat": None, "lon": None}


def test_runtime_env_beats_manifest(tmp_path, monkeypatch):
    # runtime (injected env) outranks the manifest per precedence tier 2 > 3.
    path = write_manifest(tmp_path, MANIFEST)   # manifest says H00F / its coords
    monkeypatch.setenv("WAGGLE_NODE_VSN", "W123")
    monkeypatch.setenv("WAGGLE_NODE_GPS_LAT", "10.0")
    monkeypatch.setenv("WAGGLE_NODE_GPS_LON", "20.0")
    ident = nodemeta.resolve_identity(manifest_path=path)
    assert ident["vsn"] == "W123"      # env runtime > manifest
    assert ident["lat"] == 10.0
    assert ident["lon"] == 20.0


def test_explicit_beats_runtime_env(tmp_path, monkeypatch):
    # explicit CLI arg (tier 1) still outranks the injected env (tier 2).
    monkeypatch.setenv("WAGGLE_NODE_VSN", "W123")
    ident = nodemeta.resolve_identity(
        vsn="W999", manifest_path=str(tmp_path / "none.json"))
    assert ident["vsn"] == "W999"


def test_placeholder_vsn_env_override(tmp_path, monkeypatch):
    monkeypatch.setattr(nodemeta, "DEFAULT_VSN_FILE", str(tmp_path / "no-vsn"))
    monkeypatch.setattr(nodemeta, "PLACEHOLDER_VSN", "TESTVSN")
    ident = nodemeta.resolve_identity(manifest_path=str(tmp_path / "no-manifest.json"))
    assert ident["vsn"] == "TESTVSN"
    assert ident["vsn_is_placeholder"] is True


def test_vsn_file_fallback(tmp_path, monkeypatch):
    vf = tmp_path / "vsn"; vf.write_text("W042\n")
    idf = tmp_path / "node-id"; idf.write_text("ABCD1234\n")
    monkeypatch.setattr(nodemeta, "DEFAULT_VSN_FILE", str(vf))
    monkeypatch.setattr(nodemeta, "DEFAULT_NODE_ID_FILE", str(idf))
    ident = nodemeta.resolve_identity(manifest_path=str(tmp_path / "no-manifest.json"))
    assert ident["vsn"] == "W042"       # from file (manifest absent)
    assert ident["node_id"] == "ABCD1234"


def test_manifest_beats_vsn_file(tmp_path, monkeypatch):
    vf = tmp_path / "vsn"; vf.write_text("WRONG\n")
    monkeypatch.setattr(nodemeta, "DEFAULT_VSN_FILE", str(vf))
    path = write_manifest(tmp_path, MANIFEST)
    ident = nodemeta.resolve_identity(manifest_path=path)
    assert ident["vsn"] == "H00F"       # manifest preferred over the vsn file
