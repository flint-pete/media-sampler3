#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- audio metadata (audio Stage 1).
#
# Audio has no EXIF, so provenance rides in a SIDECAR JSON next to the clip:
#     <ts>-v2-<vsn>-<source>.flac        the audio bytes
#     <ts>-v2-<vsn>-<source>.flac.json   the full v2 field dict
#
# The field dict is a SUPERSET of the shared image v2 set (metadata.build_field_dict):
# same keys, plus media_type/source/source_type. `source` is the per-stream label
# (a mic, not a camera) and fills the name-scheme slot; `camera` mirrors it so a
# legacy image consumer keying off `camera` still finds the stream label.
# unique_id = SHA256 of the ORIGINAL clip bytes (same rule as images).

import json
import os

import metadata

# Controlled vocabulary for the "how" of a capture (design §8 decision 2).
SOURCE_TYPES = frozenset({
    "camera_still", "camera_mic", "usb_mic", "rtsp_audio", "file",
})

AUDIO_FORMATS = {"flac": ".flac", "wav": ".wav"}

sha256_hex = metadata.sha256_hex  # re-export: unique_id = sha256(raw clip)


def build_audio_name(capture_ts_ns, vsn, source, *, fmt="flac"):
    """v2 clip name: <ts>-v2-<vsn>-<source>.<ext>. Reuses the image name guards
    (positive-int ts; no path separators/whitespace in vsn/source)."""
    try:
        ext = AUDIO_FORMATS[fmt]
    except KeyError:
        raise ValueError(f"audio format {fmt!r} not in {sorted(AUDIO_FORMATS)}")
    return metadata.build_v2_name(capture_ts_ns, vsn, source, ext=ext)


def build_audio_field_dict(*, vsn, node_id, job, task, plugin, source,
                           source_type, capture_ts_ns, upload_ts_ns, lat, lon,
                           unique_id, fmt="flac"):
    """The audio v2 field dict: the shared image set + media_type/source/source_type.

    `source` fills the `camera` slot (name + back-compat alias). `source_type` must
    be in SOURCE_TYPES. `acquisition_path` is fixed "native-raw" (clip captured as-is;
    FLAC is lossless so no information-losing re-encode).
    """
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type {source_type!r} not in {sorted(SOURCE_TYPES)}")

    # Reuse the shared builder with source-as-camera, then extend + retarget the name.
    d = metadata.build_field_dict(
        vsn=vsn, node_id=node_id, job=job, task=task, plugin=plugin, camera=source,
        capture_ts_ns=capture_ts_ns, upload_ts_ns=upload_ts_ns, lat=lat, lon=lon,
        acquisition_path="native-raw", unique_id=unique_id)
    d["object_name"] = build_audio_name(capture_ts_ns, vsn, source, fmt=fmt)
    d["media_type"] = "audio"
    d["source"] = source
    d["source_type"] = source_type
    return d


def sidecar_name_for(clip_path):
    """The sidecar path for a clip: <clip>.json."""
    return clip_path + ".json"


def write_sidecar(clip_path, field_dict):
    """Write field_dict as the clip's sidecar JSON (0644, world-readable for
    cross-user cache reads). Returns the sidecar path."""
    sidecar = sidecar_name_for(clip_path)
    blob = json.dumps(field_dict, separators=(",", ":"), sort_keys=True).encode("ascii")
    fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(sidecar, 0o644)  # defeat umask (O_CREAT mode is umask-masked)
    return sidecar
