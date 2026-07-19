# media-sampler3

`media-sampler3` is a versatile **media producer** for Sage/Waggle edge nodes. It
captures **still images** (JPEG) from cameras and **audio clips** (FLAC/WAV) from
microphones, and either uploads a single sample to the cloud or maintains a
bounded local **ring cache** that other plugins (inference, triggered uploaders)
consume on their own schedule.

It is an enhanced fork of the Sage/Waggle `imagesampler`, redesigned around a
**producer/consumer** model and generalized from images-only to a multi-media
producer. The producer is cheap and CPU-only; a companion consumer plugin does the
heavy work (e.g. BirdNET on audio clips, a detector on image frames).

Collecting images and audio is a fundamental way to gather training data and to
capture the sensory context in which an inference was made — a bird detected in a
clip, an object seen in a frame.

> Status: **v0.1.0.** The image path is shipped and verified end-to-end on a live
> node (H00F). The audio path is code-complete and unit-verified; on-node hardware
> validation is the remaining step. 232 tests passing. Full audio design:
> `docs/AUDIO-EXTENSION-DESIGN.md`.

---

## The self-describing v2 cache-frame

Every captured artifact is named at capture time and carries its own complete
provenance, so a consumer needs no external database:

```
<capture_ts_ns>-v2-<vsn>-<source>.<ext>       # .jpg | .flac | .wav
```

- **Images** embed the full field set as **EXIF** inside the JPEG (standard tags +
  a JSON blob in `UserComment`; SHA-256 in `ImageUniqueID`), with no pixel
  re-encode.
- **Audio** carries the same field set in a **sidecar JSON** (`<clip>.json`) next
  to the clip, because audio has no EXIF. One logical frame = clip + sidecar; the
  ring counts/evicts by the clip and writes the sidecar first so a consumer that
  sees a clip always finds its metadata.

Audio frames add `media_type`, `source` (the stream label), and `source_type`
(`camera_still | camera_mic | usb_mic | rtsp_audio | file`) to the shared field
set; `camera` is retained as an alias of `source` for legacy image consumers.

---

## Modes (exactly one is required)

| Mode | What it does |
|------|--------------|
| `--one-shot` | Capture **one** frame, queue it for cloud upload, exit. Upload-only — never writes the cache. Cadence is external (the scheduler relaunches the pod). |
| `--continuous SECONDS` | Run forever, capturing every `SECONDS`. **Local-only** — writes each frame into the ring cache and **never uploads**. This is the producer that fills the shared cache. |

---

## Command-line flags

### Media type

- **`--media {image,audio}`** — media to capture. Default `image` (fully
  back-compatible with imagesampler). One media type per process.

### Audio (with `--media audio`)

- **`--audio-source SRC`** — an ALSA device (`hw:1,0`) or a credential-free URL
  (plain RTSP audio). **Not** required for `--source-type camera_mic`, whose URL is
  built internally from `--camera-host` + env credentials (no password on the CLI).
- **`--source-type TYPE`** — `camera_still | camera_mic | usb_mic | rtsp_audio |
  file`. Drives the ffmpeg input mode and is recorded in the sidecar. Default
  `usb_mic`.
- **`--clip-seconds N`** — clip duration. Defaults to the `--continuous` period so
  clips tile the timeline with no gaps.
- **`--audio-format {flac,wav}`** — container/codec. Default `flac` (lossless,
  smaller than WAV).
- **`--bandpass-fmax HZ`** — apply a lowpass at `HZ` (Nyquist guard for
  bandwidth-limited mics, e.g. `8000` for a 16 kHz camera mic). Omit for none.

> The audio capture subprocess timeout is derived automatically as
> `clip_seconds + 15s`, so a long clip is never killed before it finishes.
> `--capture-timeout` bounds the **image** still-fetch only.

### Source

- **`--stream STREAM`** *(required)* — a stream label: a camera name for images, a
  mic label for audio (e.g. `top_camera`, `north_mic`). In `--continuous`, exactly
  one stream per process (run a separate job per stream).
- **`--name NAME`** *(optional)* — display label reported for the stream; defaults
  to the stream id. If given in a multi-stream one-shot, count must match `--stream`.
- **`--from-cache DIR`** *(one-shot only)* — upload the **newest** frame already in
  cache directory `DIR` instead of hitting the source. The composable "periodic
  uploader": pair a continuous producer with a scheduled `--one-shot --from-cache`.

### Camera connection (image, and `camera_mic` audio)

The camera address may be a flag or environment variable. **Credentials are
environment-only** (`CAMERA_USER` / `CAMERA_PASSWORD`) — never flags — so they do
not appear in process arguments, shell history, or logs (redacted in log output).

- **`--camera-host HOST`** — camera IP/host (default env `CAMERA_HOST`).
- **`--camera-port PORT`** — camera HTTP port (default env `CAMERA_PORT` or `80`;
  Reolink is typically `10000`).
- **`--camera-channel N`** — camera channel (default env `CAMERA_CHANNEL` or `0`).
- **`--capture-timeout SECONDS`** — hard timeout for a single **image** still-fetch
  (default `10`). Does not apply to audio.
- **`CAMERA_USER` / `CAMERA_PASSWORD`** *(environment only)* — camera credentials.

### Ring cache (continuous only)

- **`--cache-root DIR`** — base dir for the ring. **Optional**: defaults to
  `$IS2_CACHE_ROOT` → `/local-cache` (the shared node cache from
  `wes-local-cache-manager`). Must already exist and be writable, else fail-fast.
  The per-stream ring lives at `DIR/<cache-name>/<source>/`.
- **`--cache-name NAME`** — filesystem-safe id for this cache instance (defaults to
  the job id), so consumers can find it and two configs on one source don't
  collide. Allowed: letters, digits, `.`, `-`, `_`. No path separators/whitespace.
- **`--cache-max-count N`** — max frames kept **per stream** (evict oldest first).
- **`--cache-max-mb MB`** — max total per-stream size in decimal MB (10^6 bytes;
  for audio, includes the sidecar bytes). Evict oldest first.

  Set one or both caps; eviction fires when **either** would be exceeded. At least
  one is REQUIRED with `--continuous` (an unbounded cache is not allowed).

- **`--heartbeat-secs S`** — liveness-heartbeat cadence (default `60`), independent
  of the capture interval. Continuous mode is local-only, so the heartbeat is the
  sole liveness signal; it publishes `env.mediasampler.cache.*` stats.
- **`--max-count N` / `--max-runtime S`** — clean self-exit bounds (0 = unbounded)
  for windowed / GPU-time-shared scheduling; exit is a success.

### Node / provenance identity

Embedded in the image EXIF / audio sidecar and attached to upload meta. Resolved at
runtime from the WES-injected `WAGGLE_NODE_*` env vars (with the node manifest as an
off-node fallback), so the plugin self-identifies with no per-node config. The flags
below OVERRIDE. Identity is never fatal — Beehive attributes the node via routing.

- **`--vsn VSN`** — node VSN (e.g. `H00F`); shapes the v2 filename.
- **`--node-id ID`** — node hardware id.
- **`--lat DEG` / `--lon DEG`** — coordinates (decimal degrees); omitted if
  unresolved; negatives stored correctly (absolute value + N/S/E/W ref).
- **`--node-manifest PATH`** — manifest JSON path (default env `WAGGLE_NODE_MANIFEST`
  or `/etc/waggle/node-manifest-v2.json`). For testing / off-node use.
- **`--job NAME`** — provenance job name (default env `WAGGLE_JOB_NAME` or `sage`).
- **`--task NAME`** — task name (default env `WAGGLE_TASK_NAME` or `media-sampler3`).
- **`--plugin-version REF`** — plugin image `ref:version` recorded in provenance.

---

## Fail-fast rules

Invalid flag combinations are rejected **before any work**, with a clear message
and a config-error exit code (`2`):

- Not exactly one mode (`--one-shot` XOR `--continuous`).
- `--continuous SECONDS` not a positive integer.
- No `--stream`; or more than one `--stream` in `--continuous`.
- `--name` count does not match `--stream` count.
- `--media audio` without an audio source (`--audio-source`, or `--camera-host`
  for `camera_mic`); non-positive `--clip-seconds`.
- Audio flags used with `--media image`.
- Cache flags used with `--one-shot`; `--from-cache` used with `--continuous`.
- `--continuous` with no cache cap; cache root missing/unwritable; illegal
  `--cache-name`; non-positive cap.

---

## Usage examples

Continuous audio from a USB mic (10 s FLAC clips, keep newest 500 or 2 GB):

```bash
media-sampler3 --continuous 10 \
  --media audio --audio-source hw:1,0 --source-type usb_mic \
  --stream north_mic \
  --cache-max-count 500 --cache-max-mb 2000
```

Continuous audio from a camera mic (creds from env, 8 kHz lowpass):

```bash
export CAMERA_USER=... CAMERA_PASSWORD=...
media-sampler3 --continuous 30 \
  --media audio --source-type camera_mic \
  --camera-host 10.x.x.x --camera-port 10000 --bandpass-fmax 8000 \
  --stream top_camera_mic --cache-max-mb 5000
```

Continuous images (keep newest 500 or 1 GB per stream):

```bash
export CAMERA_USER=... CAMERA_PASSWORD=...
media-sampler3 --continuous 60 \
  --stream top_camera --camera-host 10.x.x.x \
  --cache-max-count 500 --cache-max-mb 1000
```

Periodically upload the newest cached image (composition pattern — pair with a
continuous producer, schedule on a cron cadence):

```bash
media-sampler3 --one-shot --stream top_camera \
  --from-cache /local-cache/hummingcam/top_camera
```
