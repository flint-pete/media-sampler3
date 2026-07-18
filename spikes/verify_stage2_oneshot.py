#!/usr/bin/env python3
"""Stage-2 on-node verification (THROWAWAY / manual). Not product code, not part
of the CLI. Proves acquire.fetch_raw_still + metadata.embed_all work on a REAL
camera frame: EXIF embedded WITHOUT re-encoding pixels, all fields round-trip,
unique_id == SHA256(original frame), and unique_id agrees across the UserComment
JSON and the ImageUniqueID tag.

Run ON the node (camera is only reachable there), inside a venv with piexif+pillow:

    export CAMERA_HOST=10.107.0.221 CAMERA_PORT=10000 CAMERA_USER=admin
    read -r CAMERA_PASSWORD; export CAMERA_PASSWORD   # paste, don't echo
    python3 spikes/verify_stage2_oneshot.py

Exits 0 iff every check passes. Makes exactly ONE camera hit.
"""
import os
import struct
import sys

# This throwaway lives in spikes/; make the repo root importable so acquire.py /
# metadata.py resolve whether run as `python3 spikes/verify_stage2_oneshot.py`
# from the repo root or from anywhere.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire
import metadata


def pscan(b):
    """Compressed image data (SOS..EOI) -- identical iff pixels not re-encoded."""
    return b[b.index(b"\xff\xda"):]


def main():
    host = os.environ["CAMERA_HOST"]
    port = int(os.environ["CAMERA_PORT"])
    user = os.environ["CAMERA_USER"]
    pw = os.environ["CAMERA_PASSWORD"]

    url = acquire.build_reolink_snap_url(host, port, user, pw, 0)
    cap_ts = metadata.now_capture_ts_ns()
    raw = acquire.fetch_raw_still(url, 10)
    print("captured raw:", len(raw), "bytes")

    final, uid = metadata.embed_all(
        raw, vsn="H00F", node_id="00004cbb4701d16c", job="hummingbird",
        task="image-sampler2",
        plugin="registry.sagecontinuum.org/pete/image-sampler2:0.1.0",
        camera="top", capture_ts_ns=cap_ts, upload_ts_ns=None,
        lat=41.7180, lon=-87.9827, acquisition_path="native-raw")
    print("embedded:", len(final), "bytes (added %d)" % (len(final) - len(raw)))

    ok = True

    r = pscan(final) == pscan(raw)
    print("[CHECK] pixel scan identical (no re-encode):", r); ok &= r

    r = uid == metadata.sha256_hex(raw)
    print("[CHECK] unique_id == sha256(original frame):", r); ok &= r

    payload, uid_tag = metadata.read_back_fields(final)
    r = payload.get("unique_id") == uid_tag == uid
    print("[CHECK] JSON unique_id == ImageUniqueID tag == uid:", r); ok &= r

    required = ("schema_version", "vsn", "node_id", "job", "task", "plugin",
                "camera", "capture_timestamp_ns", "upload_timestamp_ns",
                "unique_id", "object_name", "lat", "lon", "acquisition_path")
    r = all(k in payload for k in required)
    print("[CHECK] all fields present:", r); ok &= r

    # segments: APP1 (EXIF) must now be present
    i, segs = 2, []
    while i < len(final) - 1:
        if final[i] != 0xFF:
            break
        m = final[i + 1]
        if m == 0xDA:
            segs.append("SOS"); break
        if 0xD0 <= m <= 0xD9:
            i += 2; continue
        ln = struct.unpack(">H", final[i + 2:i + 4])[0]
        segs.append("FF%02X" % m); i += 2 + ln
    r = "FFE1" in segs
    print("[CHECK] APP1/EXIF present:", r, segs); ok &= r

    print("\n--- fields ---")
    for k in sorted(payload):
        print("  %s: %s" % (k, payload[k]))

    print("\n=== RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES ABOVE", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
