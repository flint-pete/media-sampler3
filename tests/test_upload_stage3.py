# Stage 3 tests for the one-shot upload path (upload.py).
#
# PURE tests: no camera, no network, no pywaggle. A FAKE plugin captures the
# upload_file() call and publish() telemetry; acquire.fetch_raw_still is mocked
# to return a real tiny JPEG so metadata.embed_all works.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import acquire  # noqa: E402
import metadata  # noqa: E402
import upload  # noqa: E402

pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")


def tiny_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, "jpeg")
    return buf.getvalue()


class FakePlugin:
    """Records upload_file() and publish() calls; acts as a context manager."""
    def __init__(self):
        self.uploads = []
        self.published = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def upload_file(self, path, meta=None, timestamp=None, keep=False):
        # read bytes now (the caller deletes the temp file afterwards)
        with open(path, "rb") as f:
            data = f.read()
        self.uploads.append({"name": os.path.basename(path), "meta": meta,
                             "timestamp": timestamp, "data": data})

    def publish(self, name, value, timestamp=None, meta=None):
        self.published.append({"name": name, "value": value,
                              "timestamp": timestamp})

    def close(self):
        self.closed = True


COMMON = dict(
    capture_timeout=10, vsn="H00F", node_id="00004CBB4701D16C", job="hummingbird",
    task="image-sampler2", plugin_version="registry.example/is2:0.1.0",
    camera="top", lat=41.7179852752395, lon=-87.98271513806043)


def test_upload_happy_path(monkeypatch):
    jpeg = tiny_jpeg()
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: jpeg)
    fp = FakePlugin()
    ok, info = upload.one_shot_upload(url="http://x/snap", plugin=fp,
                                      capture_ts_ns=1783112408323875000, **COMMON)
    assert ok is True
    assert len(fp.uploads) == 1
    up = fp.uploads[0]
    # RECORD timestamp is the capture ts (capture-time keying, design 2.10)
    assert up["timestamp"] == 1783112408323875000
    # object name is the v2 name
    assert up["name"] == "1783112408323875000-v2-H00F-top.jpg"
    assert info["object_name"] == up["name"]


def test_upload_meta_all_strings(monkeypatch):
    jpeg = tiny_jpeg()
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: jpeg)
    fp = FakePlugin()
    upload.one_shot_upload(url="http://x/snap", plugin=fp,
                           capture_ts_ns=1783112408323875000, **COMMON)
    meta = fp.uploads[0]["meta"]
    # pywaggle valid_meta requires ALL values be strings
    assert all(isinstance(v, str) for v in meta.values()), meta
    assert meta["capture_timestamp"] == "1783112408323875000"
    assert "upload_timestamp" in meta and meta["upload_timestamp"].isdigit()
    assert meta["vsn"] == "H00F"
    assert meta["camera"] == "top"


def test_upload_embeds_metadata_in_bytes(monkeypatch):
    jpeg = tiny_jpeg()
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: jpeg)
    fp = FakePlugin()
    upload.one_shot_upload(url="http://x/snap", plugin=fp,
                           capture_ts_ns=1783112408323875000, **COMMON)
    # the uploaded bytes carry our EXIF/UserComment
    payload, uid = metadata.read_back_fields(fp.uploads[0]["data"])
    assert payload["vsn"] == "H00F"
    assert uid == metadata.sha256_hex(jpeg)  # unique_id = sha of original frame


def test_upload_publishes_durations_in_ns(monkeypatch):
    jpeg = tiny_jpeg()
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: jpeg)
    fp = FakePlugin()
    upload.one_shot_upload(url="http://x/snap", plugin=fp,
                           capture_ts_ns=1783112408323875000, **COMMON)
    names = {p["name"] for p in fp.published}
    assert {"plugin.duration.grab", "plugin.duration.embed",
            "plugin.duration.upload"} <= names
    # durations are integer nanoseconds
    for p in fp.published:
        assert isinstance(p["value"], int)


def test_upload_fail_soft_on_timeout(monkeypatch):
    monkeypatch.setattr(acquire, "fetch_raw_still",
                        lambda url, t: (_ for _ in ()).throw(
                            acquire.CaptureTimeout("timed out")))
    fp = FakePlugin()
    ok, info = upload.one_shot_upload(url="http://x/snap", plugin=fp, **COMMON)
    assert ok is False
    assert "timeout" in info["error"].lower()
    assert fp.uploads == []  # nothing uploaded


def test_upload_fail_soft_on_upload_error(monkeypatch):
    jpeg = tiny_jpeg()
    monkeypatch.setattr(acquire, "fetch_raw_still", lambda url, t: jpeg)

    class BoomPlugin(FakePlugin):
        def upload_file(self, *a, **k):
            raise RuntimeError("beehive down")

    ok, info = upload.one_shot_upload(url="http://x/snap", plugin=BoomPlugin(),
                                      capture_ts_ns=1783112408323875000, **COMMON)
    assert ok is False
    assert "beehive down" in info["error"]
