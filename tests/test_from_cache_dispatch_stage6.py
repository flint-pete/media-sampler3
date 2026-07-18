# Stage 6b tests: _one_shot_from_cache dispatch + exit-code mapping. Verifies
# newest selection via scan_ring, fail-fast on missing/empty dir, and runtime
# upload-failure mapping. cache_upload is monkeypatched to capture the chosen path.

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app       # noqa: E402
import metadata  # noqa: E402
import upload    # noqa: E402

piexif = pytest.importorskip("piexif")
Image = pytest.importorskip("PIL.Image")

COMMON = dict(
    vsn="H00F", node_id="00004cbb4701d16c", job="hummingbird",
    task="image-sampler2", plugin="registry.example.org/pete/image-sampler2:0.3.0",
    camera="top", upload_ts_ns=None, lat=41.7, lon=-87.9,
    acquisition_path="native-raw")


def write_cached(dir_, capture_ts_ns):
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (9, 9, 9)).save(buf, "jpeg")
    final, _ = metadata.embed_all(buf.getvalue(), capture_ts_ns=capture_ts_ns,
                                  **COMMON)
    name = metadata.build_v2_name(capture_ts_ns, "H00F", "top")
    path = os.path.join(dir_, name)
    with open(path, "wb") as fh:
        fh.write(final)
    return path


def _args(from_cache):
    ns = app.build_parser().parse_args(
        ["--one-shot", "--stream", "top", "--from-cache", from_cache])
    app.validate_args(ns)
    return ns


def test_selects_newest_and_returns_ok(tmp_path, monkeypatch):
    # three cached files; newest capture_ts must be chosen
    write_cached(str(tmp_path), 1000000000000000000)
    write_cached(str(tmp_path), 3000000000000000000)   # newest
    write_cached(str(tmp_path), 2000000000000000000)
    chosen = {}

    def fake_upload(*, path, plugin=None):
        chosen["path"] = path
        return True, {"object_name": os.path.basename(path),
                      "capture_ts_ns": 3000000000000000000, "upload_ns": 5}

    monkeypatch.setattr(upload, "cache_upload", fake_upload)
    rc = app._one_shot_from_cache(_args(str(tmp_path)))
    assert rc == app.EXIT_OK
    assert chosen["path"].endswith("3000000000000000000-v2-H00F-top.jpg")


def test_missing_dir_fail_fast(tmp_path):
    missing = os.path.join(str(tmp_path), "nope")
    rc = app._one_shot_from_cache(_args(missing))
    assert rc == app.EXIT_CONFIG_ERROR


def test_empty_dir_fail_fast(tmp_path):
    # dir exists but no v2 images -> fail-fast (resolved: surface the misconfig)
    rc = app._one_shot_from_cache(_args(str(tmp_path)))
    assert rc == app.EXIT_CONFIG_ERROR


def test_ignores_non_v2_files_when_empty(tmp_path):
    # only a non-v2 file present -> still "empty" of managed images -> fail-fast
    with open(os.path.join(str(tmp_path), "random.jpg"), "wb") as fh:
        fh.write(b"not a v2 file")
    rc = app._one_shot_from_cache(_args(str(tmp_path)))
    assert rc == app.EXIT_CONFIG_ERROR


def test_upload_failure_maps_to_capture_error(tmp_path, monkeypatch):
    write_cached(str(tmp_path), 1000000000000000000)
    monkeypatch.setattr(upload, "cache_upload",
                        lambda *, path, plugin=None: (False, {"error": "beehive down"}))
    rc = app._one_shot_from_cache(_args(str(tmp_path)))
    assert rc == app.EXIT_CAPTURE_ERROR


def test_dispatch_routes_from_cache(tmp_path, monkeypatch):
    # main() should route --one-shot --from-cache to _one_shot_from_cache
    write_cached(str(tmp_path), 1000000000000000000)
    called = {}

    def stub(args):
        called["hit"] = True
        return app.EXIT_OK

    monkeypatch.setattr(app, "_one_shot_from_cache", stub)
    rc = app.main(["--one-shot", "--stream", "top", "--from-cache", str(tmp_path)])
    assert rc == app.EXIT_OK and called.get("hit")
