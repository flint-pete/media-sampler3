#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# media-sampler3 -- audio acquisition (audio Stage 2).
#
# Capture a bounded-duration audio clip from a source and encode it to FLAC (or
# WAV), video discarded. Unlike the image path (a single HTTP GET), audio uses an
# ffmpeg subprocess: it handles the source protocols (Reolink FLV over HTTP, ALSA
# device, RTSP) and the encode in one bounded call. Error/redaction semantics
# mirror acquire.py so the capture layer feels the same across media.
#
# Credentials are NEVER hardcoded: the Reolink FLV URL is built from caller-
# supplied host/port/user/password (sourced from env on the node). The password
# rides in the URL by the camera's API design; _redact() strips it for logging.

import logging
import os
import subprocess
import urllib.parse

logger = logging.getLogger("media-sampler3.audio_acquire")

# ext by format (mirrors audio_metadata.AUDIO_FORMATS intent; kept local to avoid
# a hard import cycle -- both reference the same {flac,wav} contract).
_FORMATS = {"flac": ".flac", "wav": ".wav"}

FLAC_MAGIC = b"fLaC"
WAV_MAGIC_RIFF = b"RIFF"
WAV_MAGIC_WAVE = b"WAVE"

# Wall-clock grace added on top of clip_seconds for the ffmpeg subprocess timeout:
# a clip of N seconds needs slightly MORE than N wall-clock seconds (connect,
# encode/flush). The subprocess ceiling must therefore be clip_seconds + grace,
# NEVER the image path's --capture-timeout (which bounds a still GET and would kill
# any clip longer than it). Overridable by the caller for slow sources.
DEFAULT_GRACE_S = 15.0


class CaptureError(Exception):
    """Audio capture failed (ffmpeg missing/nonzero, unreachable, bad output)."""


class CaptureTimeout(CaptureError):
    """Capture exceeded the bounded timeout."""


def build_reolink_flv_url(host, port, user, password, channel=0):
    """Reolink FLV audio sub-stream URL with query-param auth (see sage-waggle
    reolink-audio ref). Password compared literally -> only quote URL-breaking
    chars. Sub-stream (channel0_sub) keeps bandwidth low; ffmpeg drops the video."""
    if not host:
        raise ValueError("camera host is required")
    if not user or password is None:
        raise ValueError("camera user and password are required")
    safe = "!$'()*,;=:@/~-._"
    u = urllib.parse.quote(str(user), safe=safe)
    p = urllib.parse.quote(str(password), safe=safe)
    return (f"http://{host}:{port}/flv?port=1935&app=bcs"
            f"&stream=channel{channel}_sub.bcs&user={u}&password={p}")


def _redact(url):
    """Strip the password value from a URL for safe logging."""
    try:
        parts = urllib.parse.urlsplit(url)
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        rebuilt = "&".join(f"{k}=***" if k.lower() == "password" else f"{k}={v}"
                           for k, v in pairs)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, rebuilt, parts.fragment))
    except Exception:
        return "<url>"


def looks_like_flac(data):
    return isinstance(data, (bytes, bytearray)) and data[:4] == FLAC_MAGIC


def looks_like_wav(data):
    return (isinstance(data, (bytes, bytearray)) and len(data) >= 12
            and data[:4] == WAV_MAGIC_RIFF and data[8:12] == WAV_MAGIC_WAVE)


def build_ffmpeg_cmd(*, source, source_type, out_path, clip_seconds, fmt="flac",
                     bandpass_fmax=None):
    """Assemble the bounded ffmpeg command for one clip.

    - HTTP/RTSP sources: `-i <url>` (ffmpeg auto-detects); `usb_mic`: `-f alsa -i`.
    - `-vn` drops any video; `-t <sec>` bounds duration; `-ac 1` mono (mics are).
    - optional `-af lowpass=f=<fmax>` for bandwidth-limited mics (Nyquist guard).
    - encodes to FLAC (native, lossless) or WAV. Rejects unknown formats/duration.
    """
    if fmt not in _FORMATS:
        raise ValueError(f"audio format {fmt!r} not in {sorted(_FORMATS)}")
    if not isinstance(clip_seconds, (int, float)) or clip_seconds <= 0:
        raise ValueError("clip_seconds must be a positive number")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if source_type == "usb_mic":
        cmd += ["-f", "alsa", "-i", source]
    else:  # camera_mic / rtsp_audio / http -> let ffmpeg detect from the URL
        cmd += ["-i", source]
    cmd += ["-vn", "-ac", "1", "-t", str(int(clip_seconds))]
    if bandpass_fmax:
        cmd += ["-af", f"lowpass=f={int(bandpass_fmax)}"]
    cmd += ["-c:a", "flac" if fmt == "flac" else "pcm_s16le", out_path]
    return cmd


def capture_clip(*, source, source_type, out_path, clip_seconds,
                 grace_s=DEFAULT_GRACE_S, fmt="flac", bandpass_fmax=None):
    """Record one bounded clip via ffmpeg. Returns out_path.

    The subprocess wall-timeout is derived as clip_seconds + grace_s, so it is
    ALWAYS safely larger than the clip itself (a clip of N seconds cannot be killed
    before it finishes). grace_s covers connect + encode/flush overhead. Raises
    CaptureTimeout only on a genuine overrun (source hung past clip+grace),
    CaptureError on ffmpeg-missing / nonzero exit / non-<fmt> output. Output is
    validated by magic bytes so a 0-exit-but-garbage run is caught (fail-soft at the
    caller in continuous mode).
    """
    if not isinstance(clip_seconds, (int, float)) or clip_seconds <= 0:
        raise ValueError("clip_seconds must be a positive number")
    if grace_s is None or grace_s <= 0:
        raise ValueError("grace_s must be a positive number")
    timeout_s = float(clip_seconds) + float(grace_s)
    cmd = build_ffmpeg_cmd(source=source, source_type=source_type,
                           out_path=out_path, clip_seconds=clip_seconds,
                           fmt=fmt, bandpass_fmax=bandpass_fmax)
    logger.info("capturing %ss clip: %s (subprocess timeout %.1fs)",
                clip_seconds, _redact(source), timeout_s)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        _rm(out_path)
        raise CaptureTimeout(f"capture timed out after {timeout_s}s") from e
    except FileNotFoundError as e:
        raise CaptureError("ffmpeg not found on PATH") from e
    except OSError as e:
        _rm(out_path)
        raise CaptureError(f"capture failed: {e}") from e

    if proc.returncode != 0:
        _rm(out_path)
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise CaptureError(f"ffmpeg exit {proc.returncode}: {err}")

    try:
        with open(out_path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        raise CaptureError(f"ffmpeg produced no readable output: {e}") from e
    ok = looks_like_flac(head) if fmt == "flac" else looks_like_wav(head)
    if not ok:
        _rm(out_path)
        raise CaptureError(f"ffmpeg output is not valid {fmt} (got {head[:8]!r})")
    _fsync_file(out_path)   # durable before the caller renames it into the ring
    logger.info("captured clip: %s", out_path)
    return out_path


def _fsync_file(path):
    """fsync a closed file so its bytes are durable before an atomic rename
    (symmetric with the image path's temp->fsync->rename). Best-effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
