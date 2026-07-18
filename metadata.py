#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# image-sampler2 -- metadata module (Stage 2): capture-time stamping, the v2
# filename scheme, and EXIF/UserComment embedding.
#
# Design refs: 2.9 (node-clock two timestamps), 2.10 (v2 name), 2.11 (EXIF field
# set + hybrid embed), 4.4 (piexif, verified in the Stage-1.5 spike).
#
# THE SELF-DESCRIBING FILE: a downloaded bare JPEG carries (a) standard EXIF tags
# any tool/human can read, and (b) a complete JSON blob in UserComment for a
# lossless machine round-trip of every field, plus SHA256 in ImageUniqueID as the
# construction-guaranteed key + integrity check. Embedding is done WITHOUT a pixel
# re-encode (piexif.insert), preserving any camera-authored segments.
#
# --- SHA256 / UserComment ordering note (design 2.11 resolution) --------------
# Field 9 (unique_id) is a SHA256 that must be a STABLE, construction-guaranteed
# key AND live inside the EXIF. A hash of the FINAL saved bytes cannot live inside
# those same bytes (adding the tag changes the hash -> self-reference paradox).
#
# Resolution (see OPEN note raised to Pete; documented in design 4.6):
#   - unique_id = SHA256 of the ORIGINAL captured JPEG bytes (the camera's raw
#     frame, BEFORE any EXIF injection). This is stable, reproducible from the
#     source frame, and uniquely identifies the capture. It is written to the
#     standard EXIF ImageUniqueID tag AND included in the UserComment JSON.
#   - A separate object-integrity hash (SHA256 of the FINAL saved bytes) is NOT
#     embedded; it belongs in the upload meta at Stage 3 if wanted, where it can
#     equal the stored object without paradox.
# This keeps ImageUniqueID self-consistent (equal to the hash of the source frame,
# recomputable by hashing the frame with EXIF stripped) and avoids the paradox.

import datetime
import hashlib
import io
import json

import piexif

SCHEMA_VERSION = "sage-img-1"
V2_MARKER = "v2"

# UserComment requires an 8-byte character-code prefix (Exif spec). ASCII here.
_UC_PREFIX = b"ASCII\x00\x00\x00"


def now_capture_ts_ns():
    """Authoritative capture timestamp: node monotonic wall clock in ns (2.9).

    Uses time.time_ns() via the standard library. Must be an int of nanoseconds
    since the Unix epoch and >= 2000-01-01 (pywaggle MIN_TIMESTAMP_NS, 1.7).
    """
    import time
    return time.time_ns()


def build_v2_name(capture_ts_ns, vsn, camera, ext=".jpg"):
    """Build the full v2 filename: <capture_ts_ns>-v2-<vsn>-<camera>.jpg (2.10).

    This exact name is a property of the image AT CAPTURE; both the cache path
    (sampler writes it directly) and the upload path (pywaggle, fed
    timestamp=capture_ts, prepends the same prefix) converge on it.
    """
    if not isinstance(capture_ts_ns, int) or capture_ts_ns <= 0:
        raise ValueError("capture_ts_ns must be a positive int (nanoseconds)")
    if not vsn or not camera:
        raise ValueError("vsn and camera are required for the v2 name")
    for part, val in (("vsn", vsn), ("camera", camera)):
        if any(c in str(val) for c in "/\\ \t\n"):
            raise ValueError(f"{part} '{val}' must not contain path separators/whitespace")
    return f"{capture_ts_ns}-{V2_MARKER}-{vsn}-{camera}{ext}"


def object_name_for(capture_ts_ns, vsn, camera):
    """The object-store / on-disk object name (same as the v2 filename)."""
    return build_v2_name(capture_ts_ns, vsn, camera)


def parse_v2_name(filename):
    """Parse a v2 filename -> (capture_ts_ns:int, vsn:str, camera:str) or None.

    Inverse of build_v2_name for the ring cache (2.6): recovers the capture-ts
    prefix so eviction can order oldest-first WITHOUT a stat. Returns None for any
    name that does not match the exact <ts>-v2-<vsn>-<camera>.jpg shape (callers
    treat non-matching files as unknown -- left untouched and uncounted, 2.6).
    Only the basename is considered; any directory part is ignored.
    """
    import os
    base = os.path.basename(filename)
    if not base.endswith(".jpg"):
        return None
    stem = base[:-len(".jpg")]
    # Anchor on the "-v2-" marker rather than positional splitting: the timestamp
    # is all-digits (no '-'), so the FIRST "-v2-" after the leading digits delimits
    # <ts> from <vsn>-<camera>. This is robust to hyphens in vsn or camera (which
    # build_v2_name permits -- it only forbids path separators/whitespace). We then
    # need vsn+camera; since either may contain '-', we cannot uniquely split them
    # from the name alone -- but the ring only needs the capture-ts for ordering,
    # so we return the ts authoritatively and the remainder as a best-effort
    # (vsn, camera) via a right-split that is correct for hyphenless vsn (the
    # common case). Ordering/eviction never depends on vsn/camera.
    marker = "-" + V2_MARKER + "-"
    idx = stem.find(marker)
    if idx <= 0:
        return None
    ts_str = stem[:idx]
    rest = stem[idx + len(marker):]
    if not ts_str.isdigit() or not rest:
        return None
    ts = int(ts_str)
    if ts <= 0:
        return None
    # best-effort vsn/camera split (first '-'); correct when vsn has no '-'.
    if "-" in rest:
        vsn, camera = rest.split("-", 1)
    else:
        vsn, camera = rest, ""
    return (ts, vsn, camera)


def _ns_to_exif_datetime(ts_ns):
    """(YYYY:MM:DD HH:MM:SS, subsec_str) in UTC from ns epoch (node clock)."""
    dt = datetime.datetime.fromtimestamp(ts_ns / 1e9, tz=datetime.timezone.utc)
    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    subsec = f"{int((ts_ns % 1_000_000_000) / 1000):06d}"  # microseconds, 6 digits
    return date_str, subsec


def _deg_to_dms_rationals(deg):
    """Signed degrees -> (abs) DMS as piexif rationals. Sign handled via ref.

    piexif CANNOT serialize negative lat/lon (struct.error); store the ABS value
    as ((d,1),(m,1),(s,10000)) and give N/S or E/W separately (spike finding, 4.4).
    """
    deg = abs(float(deg))
    d = int(deg)
    m = int((deg - d) * 60)
    s = round(((deg - d) * 60 - m) * 60 * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def build_field_dict(*, vsn, node_id, job, task, plugin, camera,
                     capture_ts_ns, upload_ts_ns, lat, lon, acquisition_path,
                     unique_id):
    """Assemble the design 2.11 field set (all 13 fields).

    unique_id is the SHA256 of the ORIGINAL captured frame bytes (see module
    note) -- pass it in so the JSON blob and ImageUniqueID agree. Returns a plain
    dict suitable for JSON serialization into UserComment.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "vsn": vsn,
        "node_id": node_id,
        "job": job,
        "task": task,
        "plugin": plugin,               # "<registry.../name>:<version>"
        "camera": camera,
        "capture_timestamp_ns": capture_ts_ns,
        "upload_timestamp_ns": upload_ts_ns,   # may be None until send (2.9)
        "unique_id": unique_id,                # sha256 of original frame bytes
        "object_name": object_name_for(capture_ts_ns, vsn, camera),
        "lat": lat,
        "lon": lon,
        "acquisition_path": acquisition_path,  # "native-raw" | "opencv-reencoded"
    }


def sha256_hex(data):
    """SHA256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def build_exif_bytes(fields, *, preserved_make=None):
    """Build the EXIF block (bytes) for the given field dict (design 2.11 mapping).

    Standard tags carry the human/tool-readable view; UserComment carries the full
    JSON blob (all fields except unique_id -- see module note). Returns exif_bytes
    ready for piexif.insert(). ImageUniqueID is filled later by finalize_unique_id
    (it is the SHA256 of the FINAL injected bytes).

    preserved_make: for native-raw captures, the camera's own Make if known; else
    the mapping uses "Sage/Waggle".
    """
    cap_date, cap_subsec = _ns_to_exif_datetime(fields["capture_timestamp_ns"])
    make = preserved_make if (fields["acquisition_path"] == "native-raw"
                              and preserved_make) else "Sage/Waggle"
    human = (f"Sage image-sampler2 {V2_MARKER}; vsn={fields['vsn']}; "
             f"camera={fields['camera']}; job={fields['job']}")

    zeroth = {
        piexif.ImageIFD.Make: make,
        piexif.ImageIFD.Model: str(fields["vsn"]),
        piexif.ImageIFD.Software: str(fields["plugin"]),
        piexif.ImageIFD.DateTime: cap_date,           # capture, sec resolution
        piexif.ImageIFD.ImageDescription: human,
    }

    user_comment = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: cap_date,
        piexif.ExifIFD.SubSecTimeOriginal: cap_subsec,
        piexif.ExifIFD.OffsetTimeOriginal: "+00:00",
        piexif.ExifIFD.ImageUniqueID: str(fields["unique_id"]),
        piexif.ExifIFD.UserComment: _UC_PREFIX + user_comment.encode("ascii"),
    }

    gps_ifd = {}
    if fields.get("lat") is not None and fields.get("lon") is not None:
        lat, lon = float(fields["lat"]), float(fields["lon"])
        gps_ifd = {
            piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
            piexif.GPSIFD.GPSLatitude: _deg_to_dms_rationals(lat),
            piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
            piexif.GPSIFD.GPSLongitude: _deg_to_dms_rationals(lon),
        }

    exif_dict = {"0th": zeroth, "Exif": exif_ifd, "GPS": gps_ifd,
                 "1st": {}, "thumbnail": None}
    return piexif.dump(exif_dict)


def inject_exif(jpeg_bytes, exif_bytes):
    """Insert exif_bytes into jpeg_bytes WITHOUT re-encoding pixels (piexif).

    Returns the new JPEG bytes. Foreign camera segments are preserved (piexif
    inserts APP1 and pushes existing segments down -- verified in the 1.5 spike).
    """
    sink = io.BytesIO()
    piexif.insert(exif_bytes, jpeg_bytes, sink)
    return sink.getvalue()


def embed_all(jpeg_bytes, *, vsn, node_id, job, task, plugin, camera,
              capture_ts_ns, upload_ts_ns, lat, lon, acquisition_path,
              preserved_make=None):
    """Full Stage-2 embed: compute unique_id -> build EXIF -> inject (one pass).

    unique_id = SHA256 of the ORIGINAL captured frame bytes (jpeg_bytes as passed
    in, before injection). It goes into BOTH the UserComment JSON and the standard
    ImageUniqueID tag, so they agree and there is no self-reference paradox (see
    module note). Pixels are never re-encoded.

    Returns (final_bytes, unique_id_hex). `final_bytes` is what the caller saves.
    """
    unique_id = sha256_hex(jpeg_bytes)
    fields = build_field_dict(
        vsn=vsn, node_id=node_id, job=job, task=task, plugin=plugin, camera=camera,
        capture_ts_ns=capture_ts_ns, upload_ts_ns=upload_ts_ns, lat=lat, lon=lon,
        acquisition_path=acquisition_path, unique_id=unique_id)
    exif_bytes = build_exif_bytes(fields, preserved_make=preserved_make)
    final_bytes = inject_exif(jpeg_bytes, exif_bytes)
    return final_bytes, unique_id


def read_back_fields(jpeg_bytes):
    """Read our fields back for verification: (json_dict, image_unique_id)."""
    exif_dict = piexif.load(jpeg_bytes)
    uc = exif_dict["Exif"].get(piexif.ExifIFD.UserComment, b"")
    if uc[:8] == _UC_PREFIX:
        uc = uc[8:]
    payload = json.loads(uc.decode("ascii")) if uc else {}
    uid = exif_dict["Exif"].get(piexif.ExifIFD.ImageUniqueID, b"")
    if isinstance(uid, bytes):
        uid = uid.decode("ascii", "replace")
    return payload, uid
