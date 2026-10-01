# media-sampler3

A versatile media **producer** for Sage/Waggle edge nodes. It captures **JPEG
still images** and **audio clips** from a node's cameras and microphones and
either uploads them to the cloud or maintains a bounded local **ring cache** that
other plugins (inference, triggered uploaders) consume on their own schedule.

Enhanced fork of the Sage/Waggle **imagesampler** plugin
(`waggle-sensor/plugin-imagesampler`), generalized from images-only to a
multi-media producer built around one self-describing **v2 cache-frame contract**.

## Status

**v0.1.0 — image AND audio paths built, tested, and verified on H00F. Image path
is IN PRODUCTION.** 237 tests passing (8 image tests skip offline: no PIL in the
test venv).

As of **2026-07-23**, media-sampler3 **replaced the prior image producer** as the
live image producer on H00F (the "great cut-over") — feeding the
sage-yolo2 detector → sage-bioclip2 species-classifier cascade (an example
consumer pipeline, here used for bird detection) from the shared `/local-cache`.
Confirmed drop-in: identical `<ts>-v2-<vsn>-<name>.jpg` naming,
4K EXIF-tagged JPEGs, and yolo2 consumes ms3 frames with cloud `env.count.*`
publish. If the node reboots, follow the recovery runbook (see the
sage-design-planning reboot/recovery stack doc).

- **Image capture** (inherited from imagesampler v0.5.1, fully proven): single
  real capture, capture-time v2 naming + self-describing EXIF/JSON embed, one-shot
  upload to Beehive, `--continuous` ring cache, cache heartbeat/liveness,
  `--from-cache` uploader, and self-exit bounds for GPU time-sharing.
- **Audio capture** (new in media-sampler3): bounded FLAC/WAV clips captured via
  ffmpeg into the same ring, each with a `<clip>.json` metadata sidecar.
  **On-node validated on H00F 2026-07-19** — live Reolink camera-mic capture,
  ring cap + oldest-first eviction, heartbeat on its own grid, and cross-user
  cache reads all confirmed on real aarch64 hardware. See `RESUME-HERE.md` for the
  full validation record and the remaining deployment step (Step 4).

The producer/consumer loop uses the shared `/local-cache` node cache provided by
the `wes-local-cache-manager` WES component; if that mount is absent the plugin
**fails fast** rather than writing to an ephemeral path no consumer can read.

Full audio design: `docs/AUDIO-EXTENSION-DESIGN.md`. Original code study:
`docs/mediasampler.analysis.txt`.

## Core concepts

**Media type.** The producer captures one media type per process, selected with
`--media {image,audio}` (default `image`, fully back-compatible). One plugin
instance = one stream (one camera OR one mic); run a separate job per stream.

**The v2 cache-frame contract.** Every captured artifact is *self-describing* and
named at capture time:

```
<capture_ts_ns>-v2-<vsn>-<source>.<ext>
```

- `capture_ts_ns` — node wall-clock nanoseconds at capture (orders the ring, no
  `stat` needed).
- `vsn` — the node's VSN (e.g. `H00F`).
- `source` — the stream label (a camera name for images, a mic label for audio).
- `<ext>` — `.jpg` (image), `.flac` / `.wav` (audio).

**How provenance travels with the artifact** — the whole field set (schema,
identity, job/task, timestamps, GPS, a SHA-256 of the original bytes, etc.) is
carried *with* every frame so a downstream consumer needs no external database:

| Media | Carrier | Detail |
|-------|---------|--------|
| Image | **EXIF**, in the JPEG itself | standard tags + a full JSON blob in `UserComment`; `unique_id` in `ImageUniqueID`. No pixel re-encode. |
| Audio | **Sidecar JSON**, `<clip>.json` next to the clip | same field set (audio has no EXIF). Format-agnostic; readable by any JSON parser. |

For audio, one logical frame = **clip + its sidecar**. The ring counts and evicts
by the clip; on write it publishes the **sidecar first, then the clip**, so a
consumer that sees a clip is guaranteed its sidecar already exists.

**Audio source fields.** A mic is not a camera, so audio frames carry:
`media_type` (`image`/`audio`), `source` (the stream label), and `source_type`
(`camera_still | camera_mic | usb_mic | rtsp_audio | file`). `camera` is retained
as an alias of `source` so legacy image consumers keep working unchanged.

## Modes (exactly one is required)

| Mode | What it does |
|------|--------------|
| `--one-shot` | Capture **one** frame, queue it for cloud upload, exit. Upload-only — never writes the cache. Cadence is external (the scheduler relaunches the pod). |
| `--continuous SECONDS` | Run forever, capturing every `SECONDS`. **Local-only** — writes each frame into the ring cache and **never uploads**. This is the producer that fills the shared cache. |

## Quick start

**Continuous audio from a USB microphone** (10-second FLAC clips, keep the newest
500 or 2 GB, whichever comes first):

```bash
media-sampler3 --continuous 10 \
  --media audio --audio-source hw:1,0 --source-type usb_mic \
  --stream north_mic \
  --cache-max-count 500 --cache-max-mb 2000
```

**Continuous audio from a camera microphone** (credentials from the environment,
never on the command line — a bandwidth-limited 16 kHz mic gets an 8 kHz lowpass):

```bash
export CAMERA_USER=... CAMERA_PASSWORD=...
media-sampler3 --continuous 30 \
  --media audio --source-type camera_mic \
  --camera-host 10.x.x.x --camera-port 10000 \
  --bandpass-fmax 8000 \
  --stream top_camera_mic \
  --cache-max-mb 5000
```

**Continuous images** (unchanged from imagesampler):

```bash
export CAMERA_USER=... CAMERA_PASSWORD=...
media-sampler3 --continuous 60 \
  --stream top_camera --camera-host 10.x.x.x \
  --cache-max-count 1000
```

## Flags

### Media & audio

| Flag | Applies to | Meaning |
|------|-----------|---------|
| `--media {image,audio}` | both | Media type to capture. Default `image`. |
| `--audio-source SRC` | audio | ALSA device (`hw:1,0`) or a credential-free URL (plain RTSP). **Not** needed for `--source-type camera_mic` (its URL is built from `--camera-host` + env creds). |
| `--source-type TYPE` | audio | `camera_still \| camera_mic \| usb_mic \| rtsp_audio \| file`. Drives the ffmpeg input mode and is recorded in the sidecar. Default `usb_mic`. |
| `--clip-seconds N` | audio | Clip duration. Defaults to the `--continuous` period, so clips tile the timeline with no gaps. |
| `--audio-format {flac,wav}` | audio | Container/codec. Default `flac` (lossless, smaller). |
| `--bandpass-fmax HZ` | audio | Apply a lowpass at `HZ` (Nyquist guard for bandwidth-limited mics, e.g. `8000` for a 16 kHz camera mic). Omit for none. |

> **Audio timeout is automatic.** The ffmpeg subprocess ceiling is derived as
> `clip_seconds + 15s` of grace, so a long clip is never killed before it
> finishes. `--capture-timeout` governs the **image** still-fetch only.

### Source & cache (shared)

| Flag | Meaning |
|------|---------|
| `--stream ID` | Stream label (camera name or mic label). REQUIRED. In `--continuous`, exactly one. |
| `--name LABEL` | Optional display label reported for a stream (defaults to the stream id). |
| `--cache-root DIR` | Ring base dir. Defaults to `$IS2_CACHE_ROOT` → `/local-cache`. Must already exist & be writable, else fail-fast. |
| `--cache-name NAME` | Filesystem-safe cache-instance id (defaults to the job id). |
| `--cache-max-count N` / `--cache-max-mb MB` | Ring caps (evict oldest first). At least one is REQUIRED with `--continuous`. |
| `--heartbeat-secs S` | Liveness-heartbeat cadence (default 60), independent of the capture interval. |
| `--max-count N` / `--max-runtime S` | Clean self-exit bounds for windowed/GPU-shared scheduling (0 = unbounded). |

### Camera connection (image, and `camera_mic` audio)

`--camera-host` / `--camera-port` / `--camera-channel` / `--capture-timeout`.
**Credentials are environment-only** (`CAMERA_USER` / `CAMERA_PASSWORD`) — never
a flag, so no password appears in `ps`, shell history, or the pod spec.

### Node identity / provenance

`--vsn` / `--node-id` / `--job` / `--task` / `--plugin-version` / `--lat` /
`--lon` / `--node-manifest`. Identity is resolved at runtime from the WES-injected
`WAGGLE_NODE_*` env vars; these flags override. Identity is never fatal — Beehive
attributes the node via routing regardless.

## Provenance of the baseline

- Upstream: https://github.com/waggle-sensor/plugin-imagesampler
- Forked from the enhanced `imagesampler` producer at **v0.5.1** on 2026-07-15, then
  generalized to media-sampler3 (image + audio). See `CHANGELOG.md` for the full
  fork + rename record.

## Documentation

- `docs/AUDIO-EXTENSION-DESIGN.md` — the audio-producer design and locked decisions.
- `docs/mediasampler.analysis.txt` — the original upstream code study.
- `docs/IMPLEMENTATION-PLAN.md`, `docs/STAGE*-DESIGN-NOTE.md` — staged design notes.
- `readiness-gap.txt` — what remains before deployment (on-node audio validation,
  cross-user cache perms, pre-publish credential scrub of the inherited LAN IP).
- `jobs/` — ready-to-run job manifests.

## Changelog

All improvements and design changes are recorded in `CHANGELOG.md`. After each
change, add an entry under `[Unreleased]`; cut it under a version heading and bump
`sage.yaml` when a version ships.
