#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- cache sidecar-pair tests (audio Stage 3).
#
# A logical audio frame = clip + <clip>.json sidecar. The ring must:
#   - count/evict by the CLIP (sidecar never a separate member),
#   - include the sidecar's bytes in the member size (byte-cap accounts for it),
#   - on commit, rename SIDECAR FIRST then clip (a consumer that sees the clip is
#     guaranteed its sidecar already exists),
#   - on eviction, delete BOTH files.

import json
import os

import cache
import metadata


def _write(path, data=b"x" * 100):
    with open(path, "wb") as f:
        f.write(data)


def test_scan_counts_clip_not_sidecar(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "1783-v2-H00F-mic.flac"))
    _write(os.path.join(d, "1783-v2-H00F-mic.flac.json"), b'{"k":1}')
    ring = cache.scan_ring(d)
    assert ring.count == 1                       # one CLIP, not two files
    assert ring.members[0].name == "1783-v2-H00F-mic.flac"


def test_scan_member_size_includes_sidecar(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "1783-v2-H00F-mic.flac"), b"A" * 100)
    _write(os.path.join(d, "1783-v2-H00F-mic.flac.json"), b"B" * 40)
    ring = cache.scan_ring(d)
    assert ring.members[0].size == 140           # clip + sidecar bytes


def test_scan_sidecar_without_clip_is_unknown(tmp_path):
    # An orphan sidecar (clip evicted mid-op) must not be a member.
    d = str(tmp_path)
    _write(os.path.join(d, "1783-v2-H00F-mic.flac.json"), b"{}")
    ring = cache.scan_ring(d)
    assert ring.count == 0


def test_commit_pair_renames_sidecar_first(tmp_path):
    d = str(tmp_path)
    clip_tmp = os.path.join(d, "1783-v2-H00F-mic.flac.tmp")
    side_tmp = os.path.join(d, "1783-v2-H00F-mic.flac.json.tmp")
    _write(clip_tmp, b"CLIP")
    _write(side_tmp, b'{"m":1}')
    plan = cache.EvictPlan(False, [])
    res = cache.commit_capture_pair(d, clip_tmp, side_tmp,
                                    "1783-v2-H00F-mic.flac", plan)
    assert res.written
    clip = os.path.join(d, "1783-v2-H00F-mic.flac")
    side = clip + ".json"
    assert os.path.exists(clip) and os.path.exists(side)
    assert json.loads(open(side).read()) == {"m": 1}
    # no .tmp litter
    assert not os.path.exists(clip_tmp) and not os.path.exists(side_tmp)


def test_commit_pair_drop_new_removes_both_tmps(tmp_path):
    d = str(tmp_path)
    clip_tmp = os.path.join(d, "c.flac.tmp")
    side_tmp = os.path.join(d, "c.flac.json.tmp")
    _write(clip_tmp)
    _write(side_tmp)
    plan = cache.EvictPlan(True, [], reason="too big")
    res = cache.commit_capture_pair(d, clip_tmp, side_tmp, "c.flac", plan)
    assert not res.written
    assert not os.path.exists(clip_tmp) and not os.path.exists(side_tmp)


def test_eviction_deletes_clip_and_sidecar(tmp_path):
    d = str(tmp_path)
    old_clip = os.path.join(d, "100-v2-H00F-mic.flac")
    old_side = old_clip + ".json"
    _write(old_clip, b"OLD")
    _write(old_side, b"{}")
    ring = cache.scan_ring(d)
    victim = ring.members[0]
    new_clip_tmp = os.path.join(d, "200-v2-H00F-mic.flac.tmp")
    new_side_tmp = os.path.join(d, "200-v2-H00F-mic.flac.json.tmp")
    _write(new_clip_tmp, b"NEW")
    _write(new_side_tmp, b"{}")
    plan = cache.EvictPlan(False, [victim])
    res = cache.commit_capture_pair(d, new_clip_tmp, new_side_tmp,
                                    "200-v2-H00F-mic.flac", plan)
    assert res.written
    assert not os.path.exists(old_clip) and not os.path.exists(old_side)
    assert old_clip in res.evicted
