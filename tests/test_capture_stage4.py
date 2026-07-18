# Stage 4b tests for the shared capture+embed body (capture.py).
#
# PURE tests: no camera/network. acquire.fetch_raw_still is monkeypatched to
# return a real tiny JPEG so metadata.embed_all works. Verifies the .tmp is
# written + fsync'd, the final v2 name is correct, EXIF round-trips, timings are
# populated, and grab/embed failures raise capture.CaptureError (fail-soft).

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire      # noqa: E402
import capture      # noqa: E402
import metadata     # noqa: E402

pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")


def tiny_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "jpeg")
    return buf.getvalue()


def _args(dest_dir, **over):
    a = dict(url="http://cam/snap", capture_timeout=5.0,
             vsn="H00F", node_id="00004cbb4701d16c", job="testjob",
             task="is2", plugin_version="registry/x:0.1.1", camera="top",
             lat=None, lon=None, dest_dir=dest_dir, capture_ts_ns=1783349740220223104)
    a.update(over)
    return a


def test_capture_writes_tmp_and_names_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    res = capture.capture_and_embed_to_tmp(**_args(str(tmp_path)))
    assert res["final_name"] == metadata.build_v2_name(1783349740220223104, "H00F", "top")
    assert res["tmp_path"] == os.path.join(str(tmp_path), res["final_name"] + ".tmp")
    assert os.path.exists(res["tmp_path"])
    assert res["final_bytes"] > 0 and res["raw_bytes"] > 0
    assert res["capture_ts_ns"] == 1783349740220223104
    assert len(res["unique_id"]) == 64          # sha256 hex
    assert res["grab_ns"] >= 0 and res["embed_ns"] >= 0


def test_capture_tmp_bytes_have_our_exif(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    res = capture.capture_and_embed_to_tmp(**_args(str(tmp_path)))
    with open(res["tmp_path"], "rb") as f:
        data = f.read()
    fields, image_unique_id = metadata.read_back_fields(data)
    assert fields["vsn"] == "H00F"
    assert fields["camera"] == "top"
    assert fields["capture_timestamp_ns"] == 1783349740220223104
    assert fields["unique_id"] == res["unique_id"]
    assert image_unique_id == res["unique_id"]


def test_capture_unique_id_is_hash_of_original_frame(tmp_path, monkeypatch):
    raw = tiny_jpeg()
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: raw)
    res = capture.capture_and_embed_to_tmp(**_args(str(tmp_path)))
    assert res["unique_id"] == metadata.sha256_hex(raw)


def test_capture_timeout_raises_captureerror(tmp_path, monkeypatch):
    def boom(url, t):
        raise acquire.CaptureTimeout("slow")
    monkeypatch.setattr(acquire, "fetch_raw_still", boom)
    with pytest.raises(capture.CaptureError) as ei:
        capture.capture_and_embed_to_tmp(**_args(str(tmp_path)))
    assert "timeout" in str(ei.value)


def test_capture_error_raises_captureerror(tmp_path, monkeypatch):
    def boom(url, t):
        raise acquire.CaptureError("bad jpeg")
    monkeypatch.setattr(acquire, "fetch_raw_still", boom)
    with pytest.raises(capture.CaptureError):
        capture.capture_and_embed_to_tmp(**_args(str(tmp_path)))


def test_capture_embed_error_raises_captureerror(tmp_path, monkeypatch):
    # non-JPEG bytes make embed_all fail -> CaptureError
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: b"not a jpeg")
    with pytest.raises(capture.CaptureError):
        capture.capture_and_embed_to_tmp(**_args(str(tmp_path)))


def test_capture_stamps_ts_when_not_injected(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    res = capture.capture_and_embed_to_tmp(**_args(str(tmp_path), capture_ts_ns=None))
    assert res["capture_ts_ns"] > 0
    # final name uses the stamped ts
    assert res["final_name"].startswith(str(res["capture_ts_ns"]) + "-v2-")


def test_capture_creates_dest_dir_if_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: tiny_jpeg())
    dest = os.path.join(str(tmp_path), "nested", "dir")
    res = capture.capture_and_embed_to_tmp(**_args(dest))
    assert os.path.exists(res["tmp_path"])
