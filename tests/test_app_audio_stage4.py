#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- app.py audio-wiring tests (audio Stage 4).
#
# CLI surface (--media/--audio-source/--source-type/--clip-seconds/--audio-format/
# --bandpass-fmax), validation rules, and the audio capture branch of the
# continuous loop (acquire + metadata + cache mocked -> no ffmpeg/hardware).

import os

import pytest

import app


def _parse(argv):
    return app.build_parser().parse_args(argv)


# --- CLI defaults + parsing -------------------------------------------------

def test_media_defaults_to_image():
    a = _parse(["--one-shot", "--stream", "cam", "--camera-host", "h"])
    assert a.media == "image"


def test_media_audio_parses():
    a = _parse(["--continuous", "10", "--stream", "mic0", "--cache-max-count", "5",
                "--media", "audio", "--audio-source", "hw:1,0",
                "--source-type", "usb_mic"])
    assert a.media == "audio"
    assert a.audio_source == "hw:1,0"
    assert a.source_type == "usb_mic"


def test_audio_format_defaults_to_flac():
    a = _parse(["--continuous", "10", "--stream", "m", "--cache-max-count", "1",
                "--media", "audio", "--audio-source", "hw:0,0"])
    assert a.audio_format == "flac"
    # clip_seconds is unset at parse time; it resolves to the --continuous period
    # at runtime (clips tile the timeline). See test_clip_seconds_defaults_to_interval.
    assert a.clip_seconds is None


# --- validation -------------------------------------------------------------

def test_audio_requires_audio_source():
    a = _parse(["--continuous", "10", "--stream", "m", "--cache-max-count", "1",
                "--media", "audio"])
    with pytest.raises(app.ConfigError):
        app.validate_args(a)


def test_audio_rejects_bad_source_type():
    # argparse choices reject an unknown source_type at parse time.
    with pytest.raises(SystemExit):
        _parse(["--continuous", "10", "--stream", "m", "--cache-max-count", "1",
                "--media", "audio", "--audio-source", "s", "--source-type", "nope"])


def test_audio_rejects_bad_format():
    with pytest.raises(SystemExit):
        _parse(["--continuous", "10", "--stream", "m", "--cache-max-count", "1",
                "--media", "audio", "--audio-source", "s", "--audio-format", "mp3"])


def test_audio_rejects_nonpositive_clip_seconds():
    a = _parse(["--continuous", "10", "--stream", "m", "--cache-max-count", "1",
                "--media", "audio", "--audio-source", "s", "--clip-seconds", "0"])
    with pytest.raises(app.ConfigError):
        app.validate_args(a)


def test_image_media_ignores_audio_flags_absent():
    # default image path still validates with no audio flags (back-compat)
    a = _parse(["--continuous", "10", "--stream", "cam", "--cache-max-count", "1",
                "--camera-host", "h"])
    app.validate_args(a)  # no raise


def test_audio_flags_rejected_in_image_mode():
    a = _parse(["--continuous", "10", "--stream", "cam", "--cache-max-count", "1",
                "--media", "image", "--audio-source", "hw:0,0"])
    with pytest.raises(app.ConfigError):
        app.validate_args(a)


# --- audio capture helper (acquire mocked) ----------------------------------

def test_audio_capture_to_tmp_produces_clip_and_sidecar(tmp_path, monkeypatch):
    dest = str(tmp_path)
    ident = {"vsn": "H00F", "node_id": "abc", "lat": 41.7, "lon": -87.9}

    # mock capture_clip to write a fake FLAC into the .tmp path it is handed
    def fake_capture_clip(**kw):
        with open(kw["out_path"], "wb") as f:
            f.write(b"fLaC" + b"\x00" * 40)
        return kw["out_path"]
    monkeypatch.setattr(app.audio_acquire, "capture_clip", fake_capture_clip)

    res = app._audio_capture_to_tmp(
        source="hw:1,0", source_type="usb_mic", clip_seconds=5,
        capture_timeout=20, fmt="flac", bandpass_fmax=None,
        vsn=ident["vsn"], node_id=ident["node_id"], job="j", task="t",
        plugin_version="p:0.1.0", stream_label="usb_mic_0",
        lat=ident["lat"], lon=ident["lon"], dest_dir=dest)

    assert res["final_name"].endswith(".flac")
    assert os.path.exists(res["clip_tmp"])
    assert os.path.exists(res["sidecar_tmp"])
    assert res["final_bytes"] > 0
    # sidecar tmp holds the v2 field dict with source/source_type
    import json
    d = json.loads(open(res["sidecar_tmp"]).read())
    assert d["media_type"] == "audio"
    assert d["source"] == "usb_mic_0"
    assert d["source_type"] == "usb_mic"


def test_audio_capture_to_tmp_propagates_capture_error(tmp_path, monkeypatch):
    def boom(**kw):
        raise app.audio_acquire.CaptureError("device busy")
    monkeypatch.setattr(app.audio_acquire, "capture_clip", boom)
    with pytest.raises(app.audio_acquire.CaptureError):
        app._audio_capture_to_tmp(
            source="s", source_type="usb_mic", clip_seconds=5, capture_timeout=10,
            fmt="flac", bandpass_fmax=None, vsn="H00F", node_id="a", job="j",
            task="t", plugin_version="p", stream_label="m", lat=None, lon=None,
            dest_dir=str(tmp_path))


# --- end-to-end: audio continuous loop writes clip+sidecar into the ring ------

class _FakeClock:
    """Virtual monotonic clock (ns); sleep() advances time (proven dual-grid pattern)."""
    def __init__(self):
        self.t = 0

    def monotonic_ns(self):
        return self.t

    def sleep(self, secs):
        self.t += int(round(secs * 1e9))


def test_continuous_audio_loop_writes_pair_to_ring(tmp_path, monkeypatch):
    import metadata
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    def fake_capture_clip(**kw):
        with open(kw["out_path"], "wb") as f:
            f.write(b"fLaC" + b"\x00" * 200)
        return kw["out_path"]
    monkeypatch.setattr(app.audio_acquire, "capture_clip", fake_capture_clip)
    # distinct capture timestamps so clip names don't collide across ticks
    seq = iter(range(1000, 2000))
    monkeypatch.setattr(metadata, "now_capture_ts_ns", lambda: next(seq))

    argv = ["--continuous", "1", "--stream", "usb_mic_0", "--cache-max-count", "3",
            "--cache-root", str(cache_root), "--cache-name", "test",
            "--media", "audio", "--audio-source", "hw:1,0",
            "--source-type", "usb_mic", "--vsn", "H00F"]
    args = app.build_parser().parse_args(argv)
    app.validate_args(args)

    clk = _FakeClock()
    rc = app._continuous_to_cache(args, max_ticks=5, plugin=object(),
                                  monotonic=clk.monotonic_ns, sleep=clk.sleep)
    assert rc == app.EXIT_OK

    sdir = cache_root / "test" / "usb_mic_0"
    clips = sorted(p.name for p in sdir.glob("*.flac"))
    sidecars = sorted(p.name for p in sdir.glob("*.flac.json"))
    assert len(clips) == 3               # 5 captures, count cap = 3 -> ring holds 3
    assert len(sidecars) == 3            # every clip has its sidecar
    for c in clips:
        assert (c + ".json") in sidecars
