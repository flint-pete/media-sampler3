# Stage 4a tests for the pure ring cache (cache.py) + metadata.parse_v2_name.
#
# PURE tests: no camera, no network, no pywaggle. Filesystem only (tmp_path).
# Cover: root auto-detect, cache-name validation, stream_dir create/writable,
# scan ordering + unknown-file handling, eviction planning for every cap combo,
# the E3 guard, evict-before-write ordering, atomic publish, and fail-soft
# eviction. Section refs point at docs/imagesampler.flint.analysis.txt 2.6.

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import cache      # noqa: E402
import metadata   # noqa: E402


# --------------------------------------------------------------------------
# metadata.parse_v2_name
# --------------------------------------------------------------------------

def test_parse_v2_name_roundtrip():
    name = metadata.build_v2_name(1783349740220223104, "H00F", "top")
    ts, vsn, camera = metadata.parse_v2_name(name)
    assert ts == 1783349740220223104
    assert vsn == "H00F"
    assert camera == "top"


def test_parse_v2_name_placeholder_vsn():
    name = metadata.build_v2_name(1000, "NODE", "top_camera")
    ts, vsn, camera = metadata.parse_v2_name(name)
    assert ts == 1000 and vsn == "NODE" and camera == "top_camera"


def test_parse_v2_name_hyphenated_camera():
    # camera may contain '-'; ts must still parse authoritatively.
    name = "1783349740220223104-v2-H00F-top-camera.jpg"
    ts, vsn, camera = metadata.parse_v2_name(name)
    assert ts == 1783349740220223104
    assert vsn == "H00F"
    assert camera == "top-camera"


@pytest.mark.parametrize("bad", [
    "notanumber-v2-H00F-top.jpg",
    "1000-v3-H00F-top.jpg",          # wrong marker
    "1000-v2-H00F-top.png",          # wrong ext
    "1000-v2-H00F-top",              # no ext
    "sample.jpg",                    # upstream flat name
    "-v2-H00F-top.jpg",              # empty ts
    "0-v2-H00F-top.jpg",             # non-positive ts
    "1000-v2-.jpg",                  # empty remainder
    ".tmp",
    "1000-v2-H00F-top.jpg.tmp",      # tmp suffix
])
def test_parse_v2_name_rejects(bad):
    assert metadata.parse_v2_name(bad) is None


def test_parse_v2_name_ignores_directory():
    p = "/tmp/whatever/1234-v2-H00F-top.jpg"
    ts, vsn, camera = metadata.parse_v2_name(p)
    assert ts == 1234


# --------------------------------------------------------------------------
# resolve_cache_root
# --------------------------------------------------------------------------

def test_resolve_root_explicit_wins(monkeypatch):
    monkeypatch.setenv("IS2_CACHE_ROOT", "/env/root")
    assert cache.resolve_cache_root("/explicit") == "/explicit"


def test_resolve_root_env(monkeypatch):
    monkeypatch.setenv("IS2_CACHE_ROOT", "/env/root")
    monkeypatch.setattr(cache.os.path, "isdir", lambda p: True)
    assert cache.resolve_cache_root(None) == "/env/root"


def test_resolve_root_default_is_local_cache(monkeypatch):
    monkeypatch.delenv("IS2_CACHE_ROOT", raising=False)
    assert cache.resolve_cache_root(None) == cache.LOCAL_CACHE_DIR


def test_resolve_root_no_probe(monkeypatch):
    # resolve is pure: it must NOT probe the filesystem (presence is enforced
    # later by assert_cache_root_available), so isdir=False still yields the default
    monkeypatch.delenv("IS2_CACHE_ROOT", raising=False)
    monkeypatch.setattr(cache.os.path, "isdir", lambda p: False)
    assert cache.resolve_cache_root(None) == cache.LOCAL_CACHE_DIR


# --------------------------------------------------------------------------
# assert_cache_root_available (fail-fast, no fallback)
# --------------------------------------------------------------------------

def test_assert_cache_root_missing_raises(monkeypatch):
    monkeypatch.setattr(cache.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(cache.os, "access", lambda p, m: False)
    with pytest.raises(cache.CacheError) as ei:
        cache.assert_cache_root_available(cache.LOCAL_CACHE_DIR)
    msg = str(ei.value)
    assert "wes-local-cache-manager" in msg
    assert cache.LOCAL_CACHE_DIR in msg


def test_assert_cache_root_not_writable_raises(monkeypatch):
    monkeypatch.setattr(cache.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(cache.os, "access", lambda p, m: False)
    with pytest.raises(cache.CacheError):
        cache.assert_cache_root_available(cache.LOCAL_CACHE_DIR)


def test_assert_cache_root_present_ok(monkeypatch):
    monkeypatch.setattr(cache.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(cache.os, "access", lambda p, m: True)
    cache.assert_cache_root_available(cache.LOCAL_CACHE_DIR)


def test_assert_cache_root_names_the_given_dir(monkeypatch):
    # an explicit --cache-root that's absent is reported by name (dev escape hatch)
    monkeypatch.setattr(cache.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(cache.os, "access", lambda p, m: False)
    with pytest.raises(cache.CacheError) as ei:
        cache.assert_cache_root_available("/nope/custom")
    assert "/nope/custom" in str(ei.value)


# --------------------------------------------------------------------------
# validate_cache_name / stream_dir
# --------------------------------------------------------------------------

@pytest.mark.parametrize("good", ["job-5671", "top_camera", "a.b.c", "H00F", "x"])
def test_validate_cache_name_ok(good):
    assert cache.validate_cache_name(good) == good


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "a b", "a\tb", "café"])
def test_validate_cache_name_rejects(bad):
    with pytest.raises(cache.CacheError):
        cache.validate_cache_name(bad)


def test_stream_dir_creates_and_returns_abs(tmp_path):
    sdir = cache.stream_dir(str(tmp_path), "job-1", "top")
    assert os.path.isdir(sdir)
    assert sdir == os.path.abspath(os.path.join(str(tmp_path), "job-1", "top"))


def test_stream_dir_rejects_bad_camera(tmp_path):
    with pytest.raises(cache.CacheError):
        cache.stream_dir(str(tmp_path), "job-1", "a/b")


def test_stream_dir_no_create(tmp_path):
    sdir = cache.stream_dir(str(tmp_path), "job-1", "top", create=False)
    assert not os.path.exists(sdir)     # not created
    assert sdir.endswith(os.path.join("job-1", "top"))


def test_stream_dir_unwritable_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cache.os, "access", lambda p, m: False)
    with pytest.raises(cache.CacheError):
        cache.stream_dir(str(tmp_path), "job-1", "top")


# --------------------------------------------------------------------------
# scan_ring
# --------------------------------------------------------------------------

def _write(sdir, name, size):
    # write `size` bytes so getsize is deterministic
    with open(os.path.join(sdir, name), "wb") as f:
        f.write(b"\x00" * size)


def test_scan_empty_missing_dir(tmp_path):
    ring = cache.scan_ring(str(tmp_path / "nope"))
    assert ring.count == 0 and ring.total_bytes == 0 and ring.members == []


def test_scan_orders_oldest_first_by_capture_ts(tmp_path):
    sdir = str(tmp_path)
    # write out of order; scan must sort by ts prefix
    _write(sdir, metadata.build_v2_name(300, "H00F", "top"), 30)
    _write(sdir, metadata.build_v2_name(100, "H00F", "top"), 10)
    _write(sdir, metadata.build_v2_name(200, "H00F", "top"), 20)
    ring = cache.scan_ring(sdir)
    assert [m.capture_ts_ns for m in ring.members] == [100, 200, 300]
    assert ring.count == 3
    assert ring.total_bytes == 60


def test_scan_unknown_files_untouched_and_uncounted(tmp_path):
    sdir = str(tmp_path)
    _write(sdir, metadata.build_v2_name(100, "H00F", "top"), 10)
    _write(sdir, "sample.jpg", 999)             # upstream flat name -> unknown
    _write(sdir, "notes.txt", 5)                # unknown
    _write(sdir, metadata.build_v2_name(200, "H00F", "top") + ".tmp", 7)  # in-flight
    ring = cache.scan_ring(sdir)
    assert ring.count == 1                       # only the one v2 file
    assert ring.total_bytes == 10                # unknown/​tmp not counted
    assert len(ring.unknown_files) == 3


# --------------------------------------------------------------------------
# plan_evictions  (pure)
# --------------------------------------------------------------------------

def _ring(sizes_and_ts):
    # build a RingState directly from (ts, size) pairs, oldest-first
    members = [cache.RingMember("/x/%d" % ts,
                                metadata.build_v2_name(ts, "H00F", "top"),
                                ts, size, ts)
               for ts, size in sizes_and_ts]
    members.sort(key=lambda m: m.sort_key)
    return cache.RingState(members, [])


def test_plan_no_caps_hit_no_eviction():
    ring = _ring([(1, 10), (2, 10)])
    plan = cache.plan_evictions(ring, 10, max_count=5, max_mb=None)
    assert plan.drop_new is False and plan.evict == []


def test_plan_count_cap_evicts_oldest():
    ring = _ring([(1, 10), (2, 10), (3, 10)])   # count 3
    plan = cache.plan_evictions(ring, 10, max_count=3, max_mb=None)
    # adding 1 -> 4 > 3, evict 1 oldest
    assert plan.drop_new is False
    assert [m.capture_ts_ns for m in plan.evict] == [1]


def test_plan_count_cap_evicts_multiple():
    ring = _ring([(1, 10), (2, 10), (3, 10), (4, 10)])  # count 4
    plan = cache.plan_evictions(ring, 10, max_count=2, max_mb=None)
    # need count+1 <= 2 -> keep at most 1 old + new; evict 3 oldest
    assert [m.capture_ts_ns for m in plan.evict] == [1, 2, 3]


def test_plan_mb_cap_evicts_by_bytes():
    # cap 1 MB = 1_000_000 B; three 400kB members = 1.2MB; add 400kB
    mb = 1
    sz = 400_000
    ring = _ring([(1, sz), (2, sz), (3, sz)])
    plan = cache.plan_evictions(ring, sz, max_count=None, max_mb=mb)
    # bytes+new must be <= 1_000_000; keep <=2 total incl new -> evict 2 oldest
    assert [m.capture_ts_ns for m in plan.evict] == [1, 2]


def test_plan_evict_on_either_cap():
    # count cap satisfied, but MB cap forces eviction
    ring = _ring([(1, 900_000)])
    plan = cache.plan_evictions(ring, 900_000, max_count=10, max_mb=1)
    assert [m.capture_ts_ns for m in plan.evict] == [1]


def test_plan_e3_guard_drops_oversized_new():
    ring = _ring([])
    plan = cache.plan_evictions(ring, 2_000_000, max_count=None, max_mb=1)
    assert plan.drop_new is True and plan.evict == []
    assert "E3" in plan.reason


def test_plan_e3_not_triggered_without_mb_cap():
    ring = _ring([])
    plan = cache.plan_evictions(ring, 999_999_999, max_count=5, max_mb=None)
    assert plan.drop_new is False and plan.evict == []


def test_plan_empty_ring_new_fits():
    ring = _ring([])
    plan = cache.plan_evictions(ring, 500_000, max_count=5, max_mb=1)
    assert plan.drop_new is False and plan.evict == []


# --------------------------------------------------------------------------
# commit_capture
# --------------------------------------------------------------------------

def _tmp_with(sdir, final_name, size):
    tmp = os.path.join(sdir, final_name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(b"\xff" * size)
    return tmp


def test_commit_writes_and_evicts_in_order(tmp_path):
    sdir = str(tmp_path)
    # existing ring of 2 at cap 2; new capture should evict oldest then publish
    _write(sdir, metadata.build_v2_name(1, "H00F", "top"), 10)
    _write(sdir, metadata.build_v2_name(2, "H00F", "top"), 10)
    ring = cache.scan_ring(sdir)
    final = metadata.build_v2_name(3, "H00F", "top")
    tmp = _tmp_with(sdir, final, 10)
    plan = cache.plan_evictions(ring, 10, max_count=2, max_mb=None)
    res = cache.commit_capture(sdir, tmp, final, plan)
    assert res.written is True
    assert os.path.exists(os.path.join(sdir, final))
    # oldest (ts=1) evicted; ts=2 and ts=3 remain
    after = cache.scan_ring(sdir)
    assert [m.capture_ts_ns for m in after.members] == [2, 3]
    # no .tmp litter
    assert not any(n.endswith(".tmp") for n in os.listdir(sdir))


def test_commit_drop_new_removes_tmp_no_write(tmp_path):
    sdir = str(tmp_path)
    final = metadata.build_v2_name(3, "H00F", "top")
    tmp = _tmp_with(sdir, final, 2_000_000)
    ring = cache.scan_ring(sdir)
    plan = cache.plan_evictions(ring, 2_000_000, max_count=None, max_mb=1)  # E3
    res = cache.commit_capture(sdir, tmp, final, plan)
    assert res.written is False
    assert not os.path.exists(os.path.join(sdir, final))
    assert not os.path.exists(tmp)          # tmp cleaned up
    assert any("E3" in w for w in res.warnings)


def test_commit_atomic_publish_no_torn_file(tmp_path):
    sdir = str(tmp_path)
    final = metadata.build_v2_name(5, "H00F", "top")
    tmp = _tmp_with(sdir, final, 12)
    ring = cache.scan_ring(sdir)
    plan = cache.plan_evictions(ring, 12, max_count=5, max_mb=None)
    res = cache.commit_capture(sdir, tmp, final, plan)
    assert res.written is True
    fp = os.path.join(sdir, final)
    assert os.path.getsize(fp) == 12        # full file, not torn
    assert not os.path.exists(tmp)


def test_commit_eviction_failure_is_fail_soft(tmp_path, monkeypatch):
    sdir = str(tmp_path)
    _write(sdir, metadata.build_v2_name(1, "H00F", "top"), 10)
    ring = cache.scan_ring(sdir)
    final = metadata.build_v2_name(2, "H00F", "top")
    tmp = _tmp_with(sdir, final, 10)
    plan = cache.plan_evictions(ring, 10, max_count=1, max_mb=None)  # evict ts=1

    real_remove = os.remove

    def flaky_remove(p):
        if p.endswith(metadata.build_v2_name(1, "H00F", "top")):
            raise OSError("boom")
        return real_remove(p)

    monkeypatch.setattr(cache.os, "remove", flaky_remove)
    res = cache.commit_capture(sdir, tmp, final, plan)
    # new file still published despite eviction failure; warning recorded
    assert res.written is True
    assert os.path.exists(os.path.join(sdir, final))
    assert any("eviction failed" in w for w in res.warnings)


def test_commit_missing_victim_counts_as_evicted(tmp_path):
    sdir = str(tmp_path)
    # plan references a victim that doesn't exist on disk (already gone)
    ghost = cache.RingMember(os.path.join(sdir, metadata.build_v2_name(1, "H00F", "top")),
                             metadata.build_v2_name(1, "H00F", "top"), 1, 10, 1)
    final = metadata.build_v2_name(2, "H00F", "top")
    tmp = _tmp_with(sdir, final, 10)
    plan = cache.EvictPlan(False, [ghost])
    res = cache.commit_capture(sdir, tmp, final, plan)
    assert res.written is True
    assert ghost.path in res.evicted        # treated as satisfied
