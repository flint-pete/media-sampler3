#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# image-sampler2 -- shared capture+embed body (Stage 4b).
#
# One code path produces a fully-formed, EXIF-embedded v2 JPEG written atomically
# to a .tmp, used by BOTH:
#   - --one-shot (upload.py): hand the .tmp to pywaggle upload_file, then delete.
#   - --continuous (cache.py ring): commit the .tmp into the per-stream ring.
# Factoring this guarantees IDENTICAL bytes / naming / EXIF across both modes
# (design 2.3/2.7/2.10) and keeps the capture logic in one place.

import logging
import os
import time

import acquire
import metadata

logger = logging.getLogger("image-sampler2.capture")


class CaptureError(Exception):
    """Runtime capture/embed failure (fail-soft: callers warn + skip)."""


def capture_and_embed_to_tmp(*, url, capture_timeout, vsn, node_id, job, task,
                             plugin_version, camera, lat, lon, dest_dir,
                             capture_ts_ns=None, acquisition_path="native-raw",
                             preserved_make=None):
    """Grab one still, embed Sage EXIF, write final bytes atomically to a .tmp.

    Phases mirror the Stage-3 one-shot body (grab -> embed) but stop BEFORE the
    sink: the caller decides whether the result is uploaded (one-shot) or committed
    into the ring (continuous). The .tmp is fsync'd and lives in dest_dir named
    "<final_name>.tmp"; dest_dir must exist and be writable (caller's job).

    Returns a dict:
        {tmp_path, final_name, final_bytes(size), raw_bytes, capture_ts_ns,
         unique_id, grab_ns, embed_ns}
    Raises capture.CaptureError on any grab/embed failure (fail-soft at the caller).
    capture_ts_ns may be injected for determinism; else stamped at grab.
    """
    # --- phase 1: grab (bounded) ---------------------------------------------
    t0 = time.time_ns()
    capture_ts_ns = capture_ts_ns or metadata.now_capture_ts_ns()
    try:
        raw = acquire.fetch_raw_still(url, capture_timeout)
    except acquire.CaptureTimeout as e:
        raise CaptureError("capture timeout: %s" % e)
    except acquire.CaptureError as e:
        raise CaptureError("capture error: %s" % e)
    grab_ns = _dur(t0)

    # --- phase 2: embed (no pixel re-encode; inject EXIF/COM) ----------------
    t1 = time.time_ns()
    try:
        final_bytes, unique_id = metadata.embed_all(
            raw, vsn=vsn, node_id=node_id, job=job, task=task,
            plugin=plugin_version, camera=camera,
            capture_ts_ns=capture_ts_ns, upload_ts_ns=None,
            lat=lat, lon=lon, acquisition_path=acquisition_path,
            preserved_make=preserved_make)
    except Exception as e:
        raise CaptureError("metadata embed error: %s" % e)
    embed_ns = _dur(t1)

    final_name = metadata.build_v2_name(capture_ts_ns, vsn, camera)

    # --- write final bytes to a .tmp in dest_dir (atomic-ready) --------------
    tmp_path = os.path.join(dest_dir, final_name + ".tmp")
    try:
        _write_tmp_fsync(final_bytes, tmp_path)
    except OSError as e:
        raise CaptureError("cannot stage tmp %r: %s" % (tmp_path, e))

    return {
        "tmp_path": tmp_path,
        "final_name": final_name,
        "final_bytes": len(final_bytes),
        "raw_bytes": len(raw),
        "capture_ts_ns": capture_ts_ns,
        "unique_id": unique_id,
        "grab_ns": grab_ns,
        "embed_ns": embed_ns,
    }


def _write_tmp_fsync(data, tmp_path):
    """Write bytes to tmp_path and fsync (durable before any rename). No rename
    here -- the caller (ring commit / upload staging) decides the final step."""
    os.makedirs(os.path.dirname(os.path.abspath(tmp_path)), exist_ok=True)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _dur(start_ns):
    return time.time_ns() - start_ns
