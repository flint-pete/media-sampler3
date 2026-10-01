#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- audio_metadata tests (audio Stage 1): the v2 field set for
# audio, the sidecar-JSON carrier, unique_id = sha256(raw clip), and the v2 name.
#
# Mirrors tests/test_metadata_stage2.py (the image path) but for the sidecar model:
# audio has no EXIF, so the full field dict is written to a `<clip>.json` sidecar.

import hashlib
import json
import os

import pytest

import audio_metadata
import metadata


RAW = b"FLAC\x00\x00\x00\x22fake-clip-bytes-for-testing" * 4
FIELDS = dict(
    vsn="H00F", node_id="00004cbb4701d16c", job="camera",
    task="media-sampler3-audio", plugin="registry/beckman/media-sampler3:0.1.0",
    source="usb_mic_0", source_type="usb_mic",
    capture_ts_ns=1_783_000_000_000_000_000, upload_ts_ns=None,
    lat=41.7179852778, lon=-87.9827151389,
)


def _field_dict(**over):
    f = dict(FIELDS)
    f.update(over)
    uid = audio_metadata.sha256_hex(RAW)
    return audio_metadata.build_audio_field_dict(unique_id=uid, **f)


# --- field dict -------------------------------------------------------------

def test_media_type_is_audio():
    d = _field_dict()
    assert d["media_type"] == "audio"


def test_source_and_source_type_present():
    d = _field_dict()
    assert d["source"] == "usb_mic_0"
    assert d["source_type"] == "usb_mic"


def test_camera_mirrors_source_for_backcompat():
    # Legacy image consumers key off `camera`; audio mirrors source into it so a
    # generic reader that only knows `camera` still gets the stream label.
    d = _field_dict()
    assert d["camera"] == d["source"] == "usb_mic_0"


def test_schema_version_is_media():
    d = _field_dict()
    assert d["schema_version"] == "sage-media-1" == metadata.SCHEMA_VERSION


def test_object_name_matches_v2_flac_name():
    d = _field_dict()
    expect = audio_metadata.build_audio_name(FIELDS["capture_ts_ns"], "H00F",
                                             "usb_mic_0", fmt="flac")
    assert d["object_name"] == expect
    assert expect == "1783000000000000000-v2-H00F-usb_mic_0.flac"


def test_source_type_rejects_unknown_vocab():
    with pytest.raises(ValueError):
        _field_dict(source_type="banana")


def test_all_image_fields_still_present():
    # The audio dict is a superset of the shared v2 fields, so a consumer written
    # for the image field set finds everything it expects.
    d = _field_dict()
    for k in ("schema_version", "vsn", "node_id", "job", "task", "plugin",
              "camera", "capture_timestamp_ns", "upload_timestamp_ns",
              "unique_id", "object_name", "lat", "lon", "acquisition_path"):
        assert k in d, f"missing shared field {k}"


# --- unique_id --------------------------------------------------------------

def test_unique_id_is_sha256_of_raw_clip():
    d = _field_dict()
    assert d["unique_id"] == hashlib.sha256(RAW).hexdigest()


# --- v2 name ----------------------------------------------------------------

def test_build_audio_name_flac():
    n = audio_metadata.build_audio_name(1783, "H00F", "top_camera_mic", fmt="flac")
    assert n == "1783-v2-H00F-top_camera_mic.flac"


def test_build_audio_name_wav():
    n = audio_metadata.build_audio_name(1783, "H00F", "usb_mic_0", fmt="wav")
    assert n == "1783-v2-H00F-usb_mic_0.wav"


def test_build_audio_name_rejects_bad_format():
    with pytest.raises(ValueError):
        audio_metadata.build_audio_name(1783, "H00F", "m", fmt="mp3")


def test_source_with_whitespace_rejected():
    with pytest.raises(ValueError):
        audio_metadata.build_audio_name(1783, "H00F", "bad source", fmt="flac")


# --- sidecar write ----------------------------------------------------------

def test_write_sidecar_creates_json_next_to_clip(tmp_path):
    clip = tmp_path / "1783-v2-H00F-usb_mic_0.flac"
    clip.write_bytes(RAW)
    d = _field_dict()
    sidecar = audio_metadata.write_sidecar(str(clip), d)
    assert sidecar == str(clip) + ".json"
    assert os.path.exists(sidecar)


def test_sidecar_content_equals_field_dict(tmp_path):
    clip = tmp_path / "c.flac"
    clip.write_bytes(RAW)
    d = _field_dict()
    sidecar = audio_metadata.write_sidecar(str(clip), d)
    loaded = json.loads(open(sidecar).read())
    assert loaded == d


def test_sidecar_is_world_readable(tmp_path):
    # Cross-user cache reads require 0644 (readiness Blocker 2 / audio §5).
    clip = tmp_path / "c.flac"
    clip.write_bytes(RAW)
    sidecar = audio_metadata.write_sidecar(str(clip), _field_dict())
    mode = os.stat(sidecar).st_mode & 0o777
    assert mode == 0o644, oct(mode)


def test_sidecar_name_for_helper():
    assert audio_metadata.sidecar_name_for("/x/c.flac") == "/x/c.flac.json"


def test_sidecar_utf8_readable_not_escaped(tmp_path):
    # Non-ASCII labels are written as readable UTF-8, not \\uXXXX escapes.
    clip = tmp_path / "c.flac"
    clip.write_bytes(RAW)
    d = _field_dict(source="mic")
    d["job"] = "caf\u00e9-monitor"
    sidecar = audio_metadata.write_sidecar(str(clip), d)
    text = open(sidecar, encoding="utf-8").read()
    assert "caf\u00e9-monitor" in text          # literal é, not \\u00e9
    assert "\\u00e9" not in text
    assert json.loads(text)["job"] == "caf\u00e9-monitor"
