# media-sampler3 — Audio Producer Extension (Design Note 1)

> **Design record.** This note was written before the audio path was built
> (2026-07-15). It is implemented in v0.1.0 and validated on H00F. The current
> behaviour and flags are in [README.md](../README.md) and
> [HOW-IT-WORKS.md](HOW-IT-WORKS.md); where this note disagrees, they win. One
> resolved point: the camera mic is read over the Reolink **HTTP-FLV** stream on
> the camera's HTTP port, not RTSP. Earlier notes referenced here live in
> [history/](history/).

Status: DRAFT (2026-07-15). Generalizes the media-sampler3 v0.5.1 producer to
also capture **audio clips** into `/local-cache` using the same v2 self-describing
cache-frame contract that images use. Additive; the image path is unchanged.

## 0. Thesis

media-sampler3 = media-sampler3 with a **second media type**. A sound clip becomes
a first-class cache artifact: `<capture_ts_ns>-v2-<vsn>-<mic>.<ext>` written into a
per-stream ring in `/local-cache`, carrying the **identical v2 field set** images
carry, so a downstream audio consumer (e.g. BirdNET) reads it the same way YOLO
reads a JPEG frame — a detector/classifier cascade that also works on audio.

## 1. What we reuse UNCHANGED (the whole point of the fork)

The media-sampler3 architecture already isolates the media-specific bits:

- `metadata.build_field_dict()` — the 13-field v2 set (schema_version, vsn,
  node_id, job, task, plugin, camera→**source**, capture_ts_ns, upload_ts_ns,
  unique_id, object_name, lat, lon, acquisition_path). **Format-agnostic — reused
  verbatim for audio.**
- `metadata.build_v2_name()` / `parse_v2_name()` — already take an `ext` param;
  audio just passes `.wav`/`.flac`. parse must learn the audio extensions.
- `cache.py` ring (write .tmp → fsync → atomic rename; evict oldest by ts prefix)
  — **format-agnostic; reused verbatim.** A sidecar means "one logical frame = 2
  files"; the ring must evict them as a pair (see §4).
- `nodemeta.py` identity, `upload.py` sink, `heartbeat.py`, `app.py` loop skeleton
  — reused; app.py gains audio CLI flags + an audio capture branch.

## 2. What's NEW (the audio-specific slice)

- `audio_acquire.py` (mirrors `acquire.py`): fetch a bounded-duration clip from a
  mic source → raw audio bytes. Sources, best→floor:
  1. **Reolink FLV sub-stream over HTTP** (per node knowledge: query-param auth,
     sub-stream for bandwidth, AAC 16 kHz → 8 kHz Nyquist, `--bandpass-fmax 8000`,
     single-quote URLs, mic-enabled check). ffmpeg extracts audio, discards video.
  2. USB mic / ALSA device (`--audio-device`).
  3. RTSP audio (fallback).
  Bounded by `--clip-seconds` and a hard `--capture-timeout`.
- `audio_metadata.py` (mirrors the embed half of `metadata.py`): given raw clip
  bytes + the field dict, produce the final artifact + a **sidecar `.json`**.

## 3. The metadata-carrying decision: SIDECAR JSON (v1), embed-ready

Audio has no EXIF. A clip is stored as two files sharing a stem:

```
<ts>-v2-<vsn>-<mic>.wav       # the audio bytes (never re-encoded when native)
<ts>-v2-<vsn>-<mic>.wav.json  # the full v2 field dict (same JSON as UserComment)
```

Chosen because it is **format-agnostic** (WAV/FLAC/OGG identical handling),
**consumer-trivial** (any reader parses JSON; no codec-specific tag library), and
**keeps the metadata model byte-identical to the image path** (same
`build_field_dict`, same field names). `unique_id` = SHA256 of the ORIGINAL clip
bytes (same rule as images; no self-reference paradox since the hash lives in the
sidecar, not the audio). The sidecar JSON filename is `<clip_name>.json`.

Design is **embed-ready**: `audio_metadata` is format-aware so FLAC/OGG Vorbis-
comment embedding can be added later as an ADDITIONAL carrier without changing the
sidecar contract (consumers keep reading the sidecar; embedding is a bonus).

## 4. Cache/ring implications (the one real subtlety)

`cache.py` currently treats each file as one frame. With a sidecar, **one logical
frame = clip + its .json**. Requirements:
- Ring **counts and evicts by the CLIP** (the `.json` rides with its clip; never
  counted as a separate frame, never orphaned).
- Eviction removes both files atomically-enough (clip then sidecar, or sidecar
  then clip — a consumer must tolerate a momentary half-state; document it).
- `parse_v2_name` must ignore `.json` sidecars when enumerating frames (treat the
  clip as authoritative; the sidecar is discovered by name = `<clip>.json`).
- Write order on commit: write `clip.tmp` + `clip.json.tmp`, fsync both, then
  rename **sidecar first, then clip** so a consumer that sees the clip is
  guaranteed its sidecar already exists (read-after-clip is always complete).

## 5. Cross-user readability (fold in the deferred image TODO)

Audio clips + sidecars must be world-readable (0644) and dirs traversable (0755)
so a consumer pod running as a different user can read them — the same open item
from media-sampler3's readiness-gap Blocker 2 (history/readiness-gap.txt; since resolved). `capture._write_tmp_fsync` already
writes 0644; verify the sidecar + ring dirs match. Resolve the probe here since we
now have a live `/local-cache` on H00F to test against.

## 6. CLI surface (additive)

```
--media {image,audio,both}      default: image (back-compat)
--audio-source <url|device>     Reolink FLV / ALSA / RTSP
--clip-seconds 10               clip duration
--audio-format {wav,flac}       default wav (native, no re-encode where possible)
--bandpass-fmax 8000            for 16 kHz-native mics (Nyquist)
--mic <name>                    the "camera"-equivalent stream label (source id)
```
`camera` field generalizes to a **source label**; keep the JSON key `camera` for
consumer back-compat OR rename to `source` (DECISION for Pete — see §8).

## 7. Testing (mirror the staged offline suites)

- `test_audio_acquire.py` — bounded fetch, timeout, mic-off detection (silent WAV).
- `test_audio_metadata.py` — sidecar JSON == field dict; unique_id = sha256(raw);
  name scheme; round-trip read-back.
- `test_cache_audio_pair.py` — ring counts by clip; evicts clip+sidecar together;
  sidecar-first rename order; parse ignores .json.
- Reuse existing image tests unchanged (proves the fork didn't regress).

## 8. DECISIONS (locked 2026-07-15, by Pete)

1. **Metadata carrier: SIDECAR JSON.** `<clip>.flac` + `<clip>.flac.json`.
2. **Audio source fields: `source` + `source_type`** (NOT `camera`). Sources are
   not just cameras — USB mics, RTSP, etc. Two-field model in the v2 JSON:
   - `media_type`: `"image" | "audio"`
   - `source`: the stream label / instance name (fills the name-scheme slot;
     e.g. `usb_mic_0`, `top_camera_mic`). Universal per-stream id for BOTH media.
   - `source_type`: controlled vocab — `camera_still | camera_mic | usb_mic |
     rtsp_audio | file`. Captures the "how" so consumers route without parsing.
   - `camera`: retained as a MIRRORED alias of `source` on the image side only,
     for back-compat with existing image consumers. New audio consumers read
     `source`/`source_type`; legacy image consumers keep reading `camera`.
3. **Audio format: FLAC** (lossless, smaller than WAV; needs an encode step, but
   no information loss — acceptable vs the image "never re-encode" rule because
   FLAC is mathematically lossless). Default `--audio-format flac`.
4. **Rename now** to media-sampler3, INCLUDING published interfaces (clean break —
   no existing media-sampler3 subscribers exist):
   - `SCHEMA_VERSION`: `sage-media-1` → `sage-media-1`.
   - Heartbeat topics: `env.mediasampler.cache.*` → `env.mediasampler.cache.*`.
   - Registry image: `beckman/media-sampler3` → `beckman/media-sampler3`.
   - Version lineage restarts at `0.1.0` (CHANGELOG credits media-sampler3 v0.5.1
     as the origin baseline).
