#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# image-sampler2 -- node identity resolution.
#
# ============================================================================
# Runtime identity is now LIVE via the WES wes-nodeinfo-injection change.
# ============================================================================
# As of 2026-07-12 a running plugin CAN learn its own VSN + GPS at runtime: the
# WES change projects the `wes-identity` ConfigMap into every plugin pod via
# `envFrom`, exposing five env vars (verified live on H00F, both tiers):
#   WAGGLE_NODE_ID  WAGGLE_NODE_VSN  WAGGLE_NODE_GPS_LAT  WAGGLE_NODE_GPS_LON
#   WAGGLE_NODE_MOBILITY
# `_runtime_identity()` below reads them (sentinel->None normalized). This is the
# Tier-1 static-identity source; a live-gpsd Tier-2 GPS() wrapper is complementary
# (wraps WAGGLE_GPS_SERVER, already injected) and out of scope here.
#
# HISTORY: before this change there was NO supported runtime lookup -- pywaggle
# 0.56 had no gps/vsn API, plugin pods had only WAGGLE_PLUGIN_*/WAGGLE_SCOREBOARD
# env and no /etc/waggle mount, and plugins let Beehive attach identity DOWNSTREAM
# via routing. That downstream attribution STILL works, so runtime identity here is
# an enrichment (shapes the v2 filename + EXIF GPS), never a correctness requirement.
# ============================================================================
#
# Resolution precedence (high -> low), applied per field:
#   1. explicit CLI flag / caller value (operator override; also used off-node)
#   2. runtime identity  -> the 5 WES-injected WAGGLE_NODE_* env vars (in-pod)
#   3. node manifest / /etc/waggle files  (works ONLY when run on the node host,
#      e.g. dev/spikes; never inside a real pod)
# Node identity is NOT required for a correct upload: Beehive attributes the node
# from message routing regardless. vsn only shapes the v2 filename; lat/lon only
# enrich EXIF GPS and are OMITTED (never faked) when unknown.

import json
import os

DEFAULT_MANIFEST_PATH = os.environ.get(
    "WAGGLE_NODE_MANIFEST", "/etc/waggle/node-manifest-v2.json")
DEFAULT_VSN_FILE = "/etc/waggle/vsn"
DEFAULT_NODE_ID_FILE = "/etc/waggle/node-id"

# Placeholder VSN used ONLY until the sage-ci runtime VSN call exists. Overridable
# via env so an operator can stamp a real vsn per job in the meantime. Deliberately
# NOT a real node name -- it must be obvious in filenames/metadata that this is a
# stand-in (e.g. "1783...-v2-NODE-top.jpg"). Beehive still attributes the real
# node via routing, so this only affects the human-facing filename.
PLACEHOLDER_VSN = os.environ.get("IS2_PLACEHOLDER_VSN", "NODE")


def _read_text(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def load_manifest(path=None):
    """Load the node manifest JSON, or {} if absent/unreadable/malformed.

    Never raises. NOTE: the manifest is only readable when running on the node
    HOST (dev/spikes); it is NOT mounted into plugin pods. See the sage-ci banner.
    """
    path = path or DEFAULT_MANIFEST_PATH
    txt = _read_text(path)
    if not txt:
        return {}
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def _runtime_identity():
    """Runtime node-identity lookup from the WES-injected env vars.

    The WES `wes-nodeinfo-injection` change (verified live on H00F 2026-07-12)
    projects the `wes-identity` ConfigMap into every plugin pod via `envFrom`, so a
    running plugin now learns its own identity + GPS from five env vars:

        WAGGLE_NODE_ID  WAGGLE_NODE_VSN  WAGGLE_NODE_GPS_LAT
        WAGGLE_NODE_GPS_LON  WAGGLE_NODE_MOBILITY

    Sentinels are normalized -> None here so nothing downstream ever sees a
    placeholder (contract mirrors pywaggle2 node_info_env.py):
      - vsn:      "0" / "" / missing            -> None
      - node_id:  "" / missing                  -> None
      - lat/lon:  by RANGE (|lat|>90, |lon|>180, covers the 999 sentinel) or
                  unparseable / "" / missing    -> None  (never fabricated)

    Returns: {"vsn": None|str, "node_id": None|str, "lat": None|float,
              "lon": None|float}
    """
    e = os.environ

    def _clean(v, sentinels=("",)):
        if v is None:
            return None
        v = v.strip()
        return None if v in sentinels else v

    def _coord(v, limit):
        if v is None or v.strip() == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if abs(f) <= limit else None

    return {
        "vsn": _clean(e.get("WAGGLE_NODE_VSN"), sentinels=("", "0")),
        "node_id": _clean(e.get("WAGGLE_NODE_ID")),
        "lat": _coord(e.get("WAGGLE_NODE_GPS_LAT"), 90.0),
        "lon": _coord(e.get("WAGGLE_NODE_GPS_LON"), 180.0),
    }


def _as_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def resolve_identity(*, vsn=None, node_id=None, lat=None, lon=None,
                     manifest_path=None):
    """Resolve node identity; ALWAYS returns usable values, never fails.

    Per-field precedence: explicit arg > runtime lookup (placeholder today) >
    node manifest / /etc/waggle files (host-only). If vsn is still unknown it
    falls back to PLACEHOLDER_VSN so the v2 filename is always well-formed
    (marked "vsn_is_placeholder": True). lat/lon that cannot be resolved come
    back as None and are simply OMITTED from EXIF GPS -- we never fabricate
    coordinates.

    Returns {vsn, node_id, lat, lon, vsn_is_placeholder}.
    """
    runtime = _runtime_identity()
    manifest = load_manifest(manifest_path)

    # vsn: explicit > runtime > manifest > /etc/waggle/vsn > PLACEHOLDER
    out_vsn = (vsn or runtime.get("vsn") or manifest.get("vsn")
               or _read_text(DEFAULT_VSN_FILE))
    vsn_is_placeholder = not bool(out_vsn)
    if vsn_is_placeholder:
        out_vsn = PLACEHOLDER_VSN

    out_node_id = (node_id or runtime.get("node_id") or manifest.get("name")
                   or _read_text(DEFAULT_NODE_ID_FILE))

    # lat/lon: explicit > runtime > manifest; None -> omitted (never faked)
    out_lat = lat if lat is not None else (
        runtime.get("lat") if runtime.get("lat") is not None
        else manifest.get("gps_lat"))
    out_lon = lon if lon is not None else (
        runtime.get("lon") if runtime.get("lon") is not None
        else manifest.get("gps_lon"))

    return {
        "vsn": out_vsn,
        "node_id": out_node_id or None,
        "lat": _as_float(out_lat),
        "lon": _as_float(out_lon),
        "vsn_is_placeholder": vsn_is_placeholder,
    }
