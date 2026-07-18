# Stage 6a tests: upload.cache_upload -- upload an already-cached v2 image with its
# ORIGINAL capture-ts preserved, faithful embedded meta, and the cached original
# left untouched (§2.8). No camera/network: a fake plugin records upload_file.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import metadata  # noqa: E402
import upload    # noqa: E402

piexif = pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")

COMMON = dict(
    vsn="H00F", node_id="00004cbb4701d16c", job="hummingbird",
    task="image-sampler2", plugin="registry.example.org/pete/image-sampler2:0.3.0",
    camera="top", upload_ts_ns=None, lat=41.7180, lon=-87.9827,
    acquisition_path="native-raw")


def make_jpeg(color=(123, 200, 50)):
    buf = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(buf, "jpeg", quality=90)
    return buf.getvalue()


def write_cached(dir_, capture_ts_ns, color=(1, 2, 3)):
    """Write a real embedded v2 file into dir_ and return (path, unique_id)."""
    raw = make_jpeg(color)
    final, uid = metadata.embed_all(raw, capture_ts_ns=capture_ts_ns,
                                    **{k: v for k, v in COMMON.items()})
    name = metadata.build_v2_name(capture_ts_ns, COMMON["vsn"], COMMON["camera"])
    path = os.path.join(dir_, name)
    with open(path, "wb") as fh:
        fh.write(final)
    return path, uid


class FakePlugin:
    def __init__(self):
        self.uploaded = []      # (path_basename, meta, timestamp)
        self.published = []

    def upload_file(self, path, meta=None, timestamp=None):
        # capture the bytes too, to prove the original is copied intact
        with open(path, "rb") as fh:
            data = fh.read()
        self.uploaded.append({"name": os.path.basename(path), "meta": meta,
                              "timestamp": timestamp, "size": len(data)})

    def publish(self, name, value, timestamp=None, meta=None):
        self.published.append((name, value))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_cache_upload_preserves_capture_ts(tmp_path):
    ts = 1783112408323875000
    path, uid = write_cached(str(tmp_path), ts)
    pl = FakePlugin()
    ok, info = upload.cache_upload(path=path, plugin=pl)
    assert ok, info
    assert len(pl.uploaded) == 1
    up = pl.uploaded[0]
    # RECORD timestamp == ORIGINAL capture ts (NOT re-stamped to now)
    assert up["timestamp"] == ts
    assert up["meta"]["capture_timestamp"] == str(ts)
    # object name keeps the original v2 capture-ts name
    assert up["name"] == f"{ts}-v2-H00F-top.jpg"
    # upload_timestamp is present, distinct, and later than capture ts
    assert up["meta"]["upload_timestamp"] != str(ts)
    assert int(up["meta"]["upload_timestamp"]) > ts


def test_cache_upload_meta_reflects_embedded_fields(tmp_path):
    ts = 1783112408323875000
    path, uid = write_cached(str(tmp_path), ts)
    pl = FakePlugin()
    ok, info = upload.cache_upload(path=path, plugin=pl)
    assert ok
    m = pl.uploaded[0]["meta"]
    assert m["vsn"] == "H00F" and m["camera"] == "top"
    assert m["acquisition_path"] == "native-raw"
    assert m["unique_id"] == uid and uid          # matches embedded ImageUniqueID
    assert m["schema_version"] == metadata.SCHEMA_VERSION
    assert m["source"] == "from-cache"


def test_cache_upload_does_not_touch_original(tmp_path):
    ts = 1783112408323875000
    path, uid = write_cached(str(tmp_path), ts)
    before = sorted(os.listdir(str(tmp_path)))
    before_bytes = os.path.getsize(path)
    pl = FakePlugin()
    upload.cache_upload(path=path, plugin=pl)
    # cache dir unchanged: same files, original still present + same size
    assert sorted(os.listdir(str(tmp_path))) == before
    assert os.path.exists(path)
    assert os.path.getsize(path) == before_bytes


def test_cache_upload_publishes_upload_duration_only(tmp_path):
    ts = 1783112408323875000
    path, _ = write_cached(str(tmp_path), ts)
    pl = FakePlugin()
    upload.cache_upload(path=path, plugin=pl)
    names = [n for n, _ in pl.published]
    assert "plugin.duration.upload" in names
    # no grab/embed phases in a from-cache run
    assert "plugin.duration.grab" not in names
    assert "plugin.duration.embed" not in names


def test_cache_upload_rejects_non_v2_name(tmp_path):
    bad = os.path.join(str(tmp_path), "not-a-v2-file.jpg")
    with open(bad, "wb") as fh:
        fh.write(make_jpeg())
    pl = FakePlugin()
    ok, info = upload.cache_upload(path=bad, plugin=pl)
    assert not ok and "not a v2" in info["error"]
    assert pl.uploaded == []


def test_cache_upload_read_error(tmp_path):
    missing = os.path.join(str(tmp_path), "1783112408323875000-v2-H00F-top.jpg")
    pl = FakePlugin()
    ok, info = upload.cache_upload(path=missing, plugin=pl)
    assert not ok and "cache read error" in info["error"]


def test_cache_upload_upload_exception_fail_soft(tmp_path):
    ts = 1783112408323875000
    path, _ = write_cached(str(tmp_path), ts)

    class Broken(FakePlugin):
        def upload_file(self, *a, **k):
            raise RuntimeError("beehive down")

    ok, info = upload.cache_upload(path=path, plugin=Broken())
    assert not ok and "upload error" in info["error"]
    # original still intact after a failed upload
    assert os.path.exists(path)
