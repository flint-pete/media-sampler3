#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# image-sampler2 -- one-shot upload path (Stage 3).
#
# Ties Stage 1 (acquire) + Stage 2 (metadata embed) into a real Beehive upload:
#   grab raw still -> embed EXIF/UserComment -> plugin.upload_file(timestamp=
#   capture_ts) -> emit plugin.duration.* phase timing.
#
# KEY pywaggle facts (verified against pywaggle source + H00F, design 2.9/2.10):
#   - upload_file(path, meta, timestamp): timestamp = timestamp or get_timestamp(),
#     so passing timestamp=capture_ts_ns makes the RECORD/OBJECT use capture time
#     (the v2 filename prefix). Storage key = <timestamp>-<sha1>.
#   - meta values MUST all be strings (pywaggle valid_meta), so everything is
#     stringified here.
#   - Node identity (vsn/lat/lon) is NOT attached by pywaggle; Beehive adds it via
#     routing. We still embed it in the file's EXIF (self-describing) via Stage 2.
#   - plugin.duration.* is published in NANOSECONDS (fleet convention).
#
# Fail-soft: a runtime capture/upload failure logs and returns a nonzero code; it
# does not throw past main(). (Config errors are caught earlier, fail-fast.)

import time

import acquire
import capture as capture_mod
import metadata


# pywaggle is only needed on-node; import lazily so unit tests can inject a fake
# plugin without the dependency installed.
def _default_plugin():
    from waggle.plugin import Plugin
    return Plugin()


def _duration_ns(start_ns):
    return time.time_ns() - start_ns


def one_shot_upload(*, url, capture_timeout, vsn, node_id, job, task, plugin_version,
                    camera, lat, lon, plugin=None, capture_ts_ns=None,
                    preserved_make=None):
    """Capture ONE still, embed metadata, and upload it with capture-time keying.

    Returns (ok: bool, info: dict). `info` carries object_name, unique_id, sizes,
    and the phase durations. Never raises on a runtime capture/upload failure --
    returns ok=False with an "error" in info so the caller maps it to an exit code.

    `plugin` may be injected (tests); otherwise a real pywaggle Plugin is created.
    `capture_ts_ns` may be injected for determinism; otherwise stamped at grab.
    """
    info = {}
    own_plugin = False
    if plugin is None:
        try:
            plugin = _default_plugin()
            own_plugin = True
        except Exception as e:  # pragma: no cover - only off-node
            return False, {"error": f"pywaggle Plugin unavailable: {e}"}

    ctx = plugin if hasattr(plugin, "__enter__") else _nullcontext(plugin)
    try:
        with ctx:
            # --- phases 1+2: grab + embed (SHARED body, Stage 4b) --------------
            # capture_and_embed_to_tmp stages an fsync'd .tmp in a private temp dir;
            # we then rename it to the final object name for pywaggle upload.
            import os
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="is2-upload-")
            try:
                cap = capture_mod.capture_and_embed_to_tmp(
                    url=url, capture_timeout=capture_timeout,
                    vsn=vsn, node_id=node_id, job=job, task=task,
                    plugin_version=plugin_version, camera=camera,
                    lat=lat, lon=lon, dest_dir=tmpdir,
                    capture_ts_ns=capture_ts_ns, acquisition_path="native-raw",
                    preserved_make=preserved_make)
            except capture_mod.CaptureError as e:
                _rmtree_quiet(tmpdir)
                return False, {"error": str(e)}

            capture_ts_ns = cap["capture_ts_ns"]
            unique_id = cap["unique_id"]
            object_name = cap["final_name"]
            grab_ns = cap["grab_ns"]
            embed_ns = cap["embed_ns"]
            info.update(object_name=object_name, unique_id=unique_id,
                        raw_bytes=cap["raw_bytes"], final_bytes=cap["final_bytes"])

            # --- phase 3: upload ----------------------------------------------
            # Rename the staged .tmp to the final object name (same temp fs), then
            # pywaggle uploads that path with capture-time keying.
            t2 = time.time_ns()
            upload_ts_ns = metadata.now_capture_ts_ns()
            staged = os.path.join(tmpdir, object_name)
            try:
                os.replace(cap["tmp_path"], staged)
                meta = {
                    "camera": str(camera),
                    "vsn": str(vsn),
                    "node_id": str(node_id or ""),
                    "job": str(job),
                    "task": str(task),
                    "plugin": str(plugin_version),
                    "capture_timestamp": str(capture_ts_ns),
                    "upload_timestamp": str(upload_ts_ns),
                    "unique_id": str(unique_id),
                    "acquisition_path": "native-raw",
                    "schema_version": metadata.SCHEMA_VERSION,
                }
                # RECORD timestamp = capture time -> object key uses capture ts.
                plugin.upload_file(staged, meta=meta, timestamp=capture_ts_ns)
            except Exception as e:
                return False, {"error": f"upload error: {e}", **info}
            finally:
                # upload_file moves/copies the file; clean the temp dir either way.
                try:
                    if os.path.exists(staged):
                        os.unlink(staged)
                    os.rmdir(tmpdir)
                except OSError:
                    pass
            upload_ns = _duration_ns(t2)

            # --- phase timing (nanoseconds, fleet convention) ------------------
            info.update(grab_ns=grab_ns, embed_ns=embed_ns, upload_ns=upload_ns,
                        capture_ts_ns=capture_ts_ns, upload_ts_ns=upload_ts_ns)
            for name, dur in (("plugin.duration.grab", grab_ns),
                              ("plugin.duration.embed", embed_ns),
                              ("plugin.duration.upload", upload_ns)):
                try:
                    plugin.publish(name, dur, timestamp=capture_ts_ns)
                except Exception:
                    # duration telemetry is best-effort; never fail the upload on it
                    pass

            return True, info
    finally:
        if own_plugin and hasattr(plugin, "close"):
            try:
                plugin.close()
            except Exception:
                pass


def _rmtree_quiet(path):
    """Remove a temp dir and its contents, ignoring errors (cleanup helper)."""
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def cache_upload(*, path, plugin=None):
    """Upload an ALREADY-CACHED v2 image, preserving its original capture-ts (§2.8).

    The cached file is a complete v2 artifact (raw camera bytes + embedded
    EXIF/UserComment written by a --continuous producer). This function does NOT
    capture from a camera and does NOT re-embed: it reads the file, recovers its
    capture timestamp from the v2 name, reads back the embedded meta, and uploads a
    COPY with the RECORD timestamp set to that ORIGINAL capture ts (never re-stamped
    to now). The cached original is never moved, mutated, or evicted (§2.8).

    Returns (ok: bool, info: dict). Never raises on a runtime read/upload failure --
    returns ok=False with an "error" in info so the caller maps it to an exit code.
    `plugin` may be injected (tests); otherwise a real pywaggle Plugin is created.
    """
    import os
    import shutil
    import tempfile

    info = {}
    base = os.path.basename(path)

    # Recover capture-ts (authoritative) from the v2 name. vsn/camera come from the
    # embedded EXIF (parse_v2_name can't split hyphenated vsn/camera reliably).
    parsed = metadata.parse_v2_name(base)
    if parsed is None:
        return False, {"error": f"not a v2 cache file: {base}"}
    capture_ts_ns = parsed[0]

    try:
        with open(path, "rb") as fh:
            jpeg_bytes = fh.read()
    except OSError as e:
        return False, {"error": f"cache read error: {e}"}

    # Read back the embedded provenance so the upload meta faithfully reflects what
    # the producer stamped (unique_id, camera, vsn, acquisition_path, ...). Fall
    # back to name-parsed vsn/camera if a field is missing.
    try:
        fields, uid = metadata.read_back_fields(jpeg_bytes)
    except Exception:
        fields, uid = {}, ""
    vsn = fields.get("vsn") or parsed[1]
    camera = fields.get("camera") or parsed[2]

    own_plugin = False
    if plugin is None:
        try:
            plugin = _default_plugin()
            own_plugin = True
        except Exception as e:  # pragma: no cover - only off-node
            return False, {"error": f"pywaggle Plugin unavailable: {e}"}

    ctx = plugin if hasattr(plugin, "__enter__") else _nullcontext(plugin)
    try:
        with ctx:
            # Upload a COPY (upload_file may move/consume the source; the cached
            # original must remain untouched -- §2.8 "does not evict/touch").
            tmpdir = tempfile.mkdtemp(prefix="is2-fromcache-")
            staged = os.path.join(tmpdir, base)
            t2 = time.time_ns()
            upload_ts_ns = metadata.now_capture_ts_ns()
            try:
                shutil.copyfile(path, staged)
                meta = {
                    "camera": str(camera),
                    "vsn": str(vsn),
                    "node_id": str(fields.get("node_id", "") or ""),
                    "job": str(fields.get("job", "") or ""),
                    "task": str(fields.get("task", "") or ""),
                    "plugin": str(fields.get("plugin", "") or ""),
                    "capture_timestamp": str(capture_ts_ns),
                    "upload_timestamp": str(upload_ts_ns),
                    "unique_id": str(uid or fields.get("unique_id", "") or ""),
                    "acquisition_path": str(fields.get("acquisition_path",
                                                       "native-raw") or "native-raw"),
                    "schema_version": metadata.SCHEMA_VERSION,
                    "source": "from-cache",
                }
                # RECORD timestamp = ORIGINAL capture time (preserve end to end).
                plugin.upload_file(staged, meta=meta, timestamp=capture_ts_ns)
            except Exception as e:
                return False, {"error": f"upload error: {e}", **info}
            finally:
                try:
                    if os.path.exists(staged):
                        os.unlink(staged)
                    os.rmdir(tmpdir)
                except OSError:
                    pass
            upload_ns = _duration_ns(t2)

            info.update(object_name=base, unique_id=str(uid or ""),
                        final_bytes=len(jpeg_bytes), capture_ts_ns=capture_ts_ns,
                        upload_ts_ns=upload_ts_ns, upload_ns=upload_ns)
            # Only the upload phase applies here (no grab/embed in a from-cache run).
            try:
                plugin.publish("plugin.duration.upload", upload_ns,
                               timestamp=capture_ts_ns)
            except Exception:
                pass
            return True, info
    finally:
        if own_plugin and hasattr(plugin, "close"):
            try:
                plugin.close()
            except Exception:
                pass


class _nullcontext:
    """Minimal contextlib.nullcontext for a plugin that isn't a context manager."""
    def __init__(self, obj):
        self.obj = obj

    def __enter__(self):
        return self.obj

    def __exit__(self, *exc):
        return False
