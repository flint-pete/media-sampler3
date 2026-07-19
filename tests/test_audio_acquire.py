#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- audio_acquire tests (audio Stage 2).
#
# Offline: we don't run ffmpeg or touch hardware. We test command construction
# (Reolink FLV / ALSA / RTSP), password redaction, format/duration validation,
# timeout plumbing, and FLAC-magic validation of the produced bytes (ffmpeg mocked).

import shutil
import subprocess

import pytest

import audio_acquire


# --- source URL / command builders ------------------------------------------

def test_reolink_flv_url_has_query_param_auth():
    url = audio_acquire.build_reolink_flv_url("10.0.0.5", 10000, "sage", "pw!42")
    assert url.startswith("http://10.0.0.5:10000/flv?")
    assert "user=sage" in url
    assert "password=pw!42" in url  # Reolink compares literally; not re-encoded


def test_reolink_flv_requires_host_and_creds():
    with pytest.raises(ValueError):
        audio_acquire.build_reolink_flv_url("", 10000, "sage", "pw")
    with pytest.raises(ValueError):
        audio_acquire.build_reolink_flv_url("h", 10000, "sage", None)


def test_redact_hides_password():
    url = audio_acquire.build_reolink_flv_url("h", 1, "sage", "SECRET99")
    red = audio_acquire._redact(url)
    assert "SECRET99" not in red
    assert "password=***" in red


def test_ffmpeg_cmd_for_http_source_extracts_audio_only():
    cmd = audio_acquire.build_ffmpeg_cmd(
        source="http://h:1/flv?x=1", source_type="camera_mic",
        out_path="/t/c.flac", clip_seconds=10, fmt="flac", bandpass_fmax=8000)
    assert cmd[0] == "ffmpeg"
    assert "-vn" in cmd                      # drop video
    assert "-t" in cmd and "10" in cmd       # bounded duration
    assert "-i" in cmd
    assert cmd[-1] == "/t/c.flac"
    assert "flac" in " ".join(cmd)


def test_ffmpeg_cmd_for_usb_mic_uses_alsa():
    cmd = audio_acquire.build_ffmpeg_cmd(
        source="hw:1,0", source_type="usb_mic",
        out_path="/t/c.flac", clip_seconds=5, fmt="flac")
    j = " ".join(cmd)
    assert "-f alsa" in j
    assert "hw:1,0" in j


def test_ffmpeg_cmd_bandpass_applied_when_set():
    cmd = audio_acquire.build_ffmpeg_cmd(
        source="s", source_type="rtsp_audio", out_path="/t/c.flac",
        clip_seconds=5, fmt="flac", bandpass_fmax=8000)
    assert "-af" in cmd
    assert "lowpass=f=8000" in cmd


def test_ffmpeg_cmd_no_bandpass_when_unset():
    cmd = audio_acquire.build_ffmpeg_cmd(
        source="s", source_type="rtsp_audio", out_path="/t/c.flac",
        clip_seconds=5, fmt="flac")
    assert "-af" not in cmd


def test_ffmpeg_cmd_rejects_bad_format():
    with pytest.raises(ValueError):
        audio_acquire.build_ffmpeg_cmd(source="s", source_type="usb_mic",
                                       out_path="/t/c.mp3", clip_seconds=5, fmt="mp3")


def test_ffmpeg_cmd_rejects_nonpositive_duration():
    with pytest.raises(ValueError):
        audio_acquire.build_ffmpeg_cmd(source="s", source_type="usb_mic",
                                       out_path="/t/c.flac", clip_seconds=0, fmt="flac")


# --- FLAC magic -------------------------------------------------------------

def test_looks_like_flac():
    assert audio_acquire.looks_like_flac(b"fLaC\x00\x00\x00\x22rest")
    assert not audio_acquire.looks_like_flac(b"RIFF....WAVE")
    assert not audio_acquire.looks_like_flac(b"")


def test_looks_like_wav():
    assert audio_acquire.looks_like_wav(b"RIFF\x24\x00\x00\x00WAVEfmt ")
    assert not audio_acquire.looks_like_wav(b"fLaC")


# --- capture_clip (ffmpeg mocked) -------------------------------------------

def _fake_run_ok(out_path, magic):
    def run(cmd, **kw):
        # simulate ffmpeg writing a valid file
        with open(cmd[-1], "wb") as f:
            f.write(magic + b"\x00" * 64)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    return run


def test_capture_clip_returns_path_on_success(tmp_path, monkeypatch):
    out = tmp_path / "1783-v2-H00F-usb_mic_0.flac"
    monkeypatch.setattr(audio_acquire.subprocess, "run",
                        _fake_run_ok(str(out), b"fLaC"))
    p = audio_acquire.capture_clip(
        source="hw:1,0", source_type="usb_mic", out_path=str(out),
        clip_seconds=5, fmt="flac")
    assert p == str(out)
    assert audio_acquire.looks_like_flac(open(p, "rb").read())


def test_capture_clip_timeout_raises(tmp_path, monkeypatch):
    out = tmp_path / "c.flac"
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
    monkeypatch.setattr(audio_acquire.subprocess, "run", boom)
    with pytest.raises(audio_acquire.CaptureTimeout):
        audio_acquire.capture_clip(source="s", source_type="usb_mic",
                                   out_path=str(out), clip_seconds=5, fmt="flac")


def test_capture_clip_nonzero_exit_raises(tmp_path, monkeypatch):
    out = tmp_path / "c.flac"
    def fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", b"device busy")
    monkeypatch.setattr(audio_acquire.subprocess, "run", fail)
    with pytest.raises(audio_acquire.CaptureError):
        audio_acquire.capture_clip(source="s", source_type="usb_mic",
                                   out_path=str(out), clip_seconds=5, fmt="flac")


def test_capture_clip_bad_output_bytes_raises(tmp_path, monkeypatch):
    # ffmpeg exits 0 but produced junk (not FLAC) -> treat as failure.
    out = tmp_path / "c.flac"
    monkeypatch.setattr(audio_acquire.subprocess, "run",
                        _fake_run_ok(str(out), b"JUNK"))
    with pytest.raises(audio_acquire.CaptureError):
        audio_acquire.capture_clip(source="s", source_type="usb_mic",
                                   out_path=str(out), clip_seconds=5, fmt="flac")


# --- timeout is derived from clip_seconds (Bug 1 fix) -----------------------

def test_subprocess_timeout_exceeds_clip_length(tmp_path, monkeypatch):
    # The subprocess timeout must ALWAYS be > clip_seconds, so a long clip is never
    # killed before it completes. Capture the timeout ffmpeg is actually run with.
    seen = {}
    out = tmp_path / "c.flac"
    def spy(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        with open(cmd[-1], "wb") as f:
            f.write(b"fLaC" + b"\x00" * 40)
        return audio_acquire.subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(audio_acquire.subprocess, "run", spy)
    audio_acquire.capture_clip(source="s", source_type="usb_mic",
                               out_path=str(out), clip_seconds=60, fmt="flac")
    assert seen["timeout"] > 60          # 60s clip is NOT killed at 10s anymore
    assert seen["timeout"] == 60 + audio_acquire.DEFAULT_GRACE_S


def test_capture_clip_rejects_nonpositive_grace(tmp_path):
    with pytest.raises(ValueError):
        audio_acquire.capture_clip(source="s", source_type="usb_mic",
                                   out_path=str(tmp_path / "c.flac"),
                                   clip_seconds=5, grace_s=0, fmt="flac")


# --- muxer is forced explicitly (Bug 6: .tmp path has no inferable extension) ---

def test_ffmpeg_cmd_forces_flac_muxer():
    cmd = audio_acquire.build_ffmpeg_cmd(
        source="hw:1,0", source_type="usb_mic",
        out_path="/t/c.flac.tmp", clip_seconds=5, fmt="flac")
    # -f flac must be present so ffmpeg does not try to infer from the .tmp path
    assert "-f" in cmd and "flac" in cmd
    fi = cmd.index("-f", cmd.index("-vn"))   # the OUTPUT -f (after -vn), not -f alsa
    assert cmd[fi + 1] == "flac"


def test_ffmpeg_cmd_forces_wav_muxer():
    cmd = audio_acquire.build_ffmpeg_cmd(
        source="hw:1,0", source_type="usb_mic",
        out_path="/t/c.wav.tmp", clip_seconds=5, fmt="wav")
    assert "pcm_s16le" in cmd
    fi = cmd.index("-f", cmd.index("-vn"))
    assert cmd[fi + 1] == "wav"


# --- REAL ffmpeg integration: prove a clip actually encodes to a .tmp path ------
# This is the class of bug unit mocks miss: ffmpeg muxer inference on the real
# binary. Uses a synthetic lavfi source, so no hardware/network is needed.

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("fmt,magic", [("flac", b"fLaC"), ("wav", b"RIFF")])
def test_real_ffmpeg_encodes_to_tmp_path(tmp_path, monkeypatch, fmt, magic):
    out = str(tmp_path / f"1783-v2-H00F-mic.{fmt}.tmp")
    # Patch build_ffmpeg_cmd to replace the input spec with a synthetic lavfi tone,
    # keeping the REAL codec/muxer/-t/out_path plumbing under test. Use a non-usb
    # source_type so the input is a clean single "-i <source>" to swap.
    real = audio_acquire.build_ffmpeg_cmd
    def synth(**kw):
        cmd = real(**kw)
        i = cmd.index("-i")
        cmd[i:i+2] = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        return cmd
    monkeypatch.setattr(audio_acquire, "build_ffmpeg_cmd", synth)
    path = audio_acquire.capture_clip(source="unused", source_type="rtsp_audio",
                                      out_path=out, clip_seconds=1, fmt=fmt)
    with open(path, "rb") as f:
        assert f.read(4) == magic
