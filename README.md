# media-sampler3

The media **producer** for Sage/Waggle edge nodes. It captures **JPEG stills**
and **audio clips** from a node's cameras and microphones into a bounded ring in
the node's shared `/local-cache`. Other plugins (detectors, classifiers,
uploaders) then read those files on their own schedule. Every file describes
itself: capture time, node, source, and a SHA-256 hash travel with it.

It is the hub of a six-component media stack:

| Start here | |
|---|---|
| [INSTALLING-MEDIA-SAMPLER3.md](INSTALLING-MEDIA-SAMPLER3.md) | Install the whole stack on a Thor node and validate it end to end |
| [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) | What each component does, why, and how they connect; includes this repo's code map |
| [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md) | **After a node reboot:** bring the stack back up (it is side-loaded and not reboot-durable) |
| [DESIGN-PATH.md](DESIGN-PATH.md) | How each component's design evolved; links to `docs/history/` |

Related repos:

- [wes-local-cache-manager](https://github.com/flint-pete/wes-local-cache-manager) bounds `/local-cache`
- [wes-nodeinfo-injection](https://github.com/flint-pete/wes-nodeinfo-injection) and [pywaggle2-nodeinfo](https://github.com/flint-pete/pywaggle2-nodeinfo) provide node identity
- [sage-yolo2](https://github.com/flint-pete/sage-yolo2) → [sage-bioclip2](https://github.com/flint-pete/sage-bioclip2) are the example consumers

## Status

**v0.1.0.** The image path has been in production on H00F since 2026-07-23. The
full cascade (producer → yolo2 → bioclip2) was re-verified on H041 with two
cameras in Sep 2026. The audio path was validated on H00F (camera mic, 15 s FLAC
once a minute). `make test`: 237 passed, 8 skipped (the skipped image tests need
PIL, which the test venv doesn't have).

## Core concepts

**One process captures one stream.** `--media image` (the default) or
`--media audio`, from one camera or one mic. Run one pod per stream.

**Two modes; exactly one is required:**

| Mode | What it does |
|------|--------------|
| `--continuous SECONDS` | Run forever, capturing every `SECONDS` into the ring cache. **Never uploads frames**; publishes only liveness heartbeats (`env.mediasampler.cache.*`). This is the producer mode the stack uses. |
| `--one-shot` | Capture one frame, queue it for Beehive upload, and exit. Never writes the cache. Add `--from-cache DIR` to upload the newest frame already in a ring instead of contacting the camera. |

**The v2 cache-frame contract.** Every artifact is named at capture time:

```
<capture_ts_ns>-v2-<vsn>-<source>.<ext>       e.g. 1790000000000000000-v2-H041-top.jpg
```

It lives in `<cache-root>/<cache-name>/<source>/`, for example
`/local-cache/camera/top/`.

- `<source>` is `--name` if given, otherwise `--stream`.
- `.jpg` for images; `.flac` or `.wav` for audio.

Provenance travels with the file, so consumers need no database:

| Media | Where the provenance is | Detail |
|-------|-------------------------|--------|
| Image | **EXIF**, inside the JPEG | standard tags plus a JSON blob in `UserComment`; `unique_id` (SHA-256 of the original bytes) in `ImageUniqueID`. Pixels are never re-encoded. |
| Audio | **Sidecar** `<clip>.json` | the same fields (`media_type=audio`, `source`, `source_type`, …). The sidecar is written before the clip. |

**The ring is bounded by the producer.** `--cache-max-count` and/or
`--cache-max-mb` (at least one is required with `--continuous`) evict the oldest
files first, at write time. Files appear atomically (written as `.tmp`, then
renamed). wes-local-cache-manager is the node-wide safety net.

**Refuses to start without the cache.** If `/local-cache` (or `--cache-root`)
isn't an existing, writable directory, `--continuous` exits with code 2. Writing
frames that no consumer could ever read would be worse than failing.

## Running it

On a node, media-sampler3 runs as a side-loaded image through `pluginctl`. The
full, verified commands are in
[INSTALLING-MEDIA-SAMPLER3.md](INSTALLING-MEDIA-SAMPLER3.md) Steps 4 and 6b. The
image producer for one camera:

```bash
make sideload        # on the node: build + import localhost/media-sampler3:<version>

sudo pluginctl run --name camera-producer --selector zone=core \
  --env-from ~/ms3-cam-creds.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/media-sampler3:0.1.0 -- \
  --continuous 10 --media image --stream top_camera --name top \
  --cache-root /local-cache --cache-name camera \
  --cache-max-count 200 --cache-max-mb 500 --heartbeat-secs 60 --vsn "$VSN" \
  --camera-host <CAM_IP> --camera-port 80
```

For local development off a node (needs Python with `pywaggle` and `piexif`, a
reachable camera, and an existing cache directory):

```bash
export CAMERA_USER=... CAMERA_PASSWORD=...
mkdir -p /tmp/lc
python3 app.py --continuous 10 --stream top --cache-root /tmp/lc --cache-name camera \
  --cache-max-count 50 --camera-host <CAM_IP> --vsn TEST
```

**Audio from a camera mic.** This is the Reolink HTTP-FLV audio sub-stream on the
camera's HTTP port, recorded with ffmpeg. Credentials come from the env:

```bash
... -- --continuous 60 --clip-seconds 15 --media audio --source-type camera_mic \
  --camera-host <CAM_IP> --camera-port 80 --bandpass-fmax 8000 \
  --stream mic --cache-name camera-audio --cache-max-count 500 --cache-max-mb 2000
```

**Audio from a USB mic** (ALSA device; find it with `arecord -l`):

```bash
... -- --continuous 10 --media audio --source-type usb_mic --audio-source hw:1,0 \
  --stream north_mic --cache-name birdaudio --cache-max-count 500
```

## Flags

### Mode, source, and naming

| Flag | Meaning |
|------|---------|
| `--continuous SECONDS` / `--one-shot` | Mode (see above). |
| `--stream ID` | Stream label (e.g. `top_camera`, `mic`). A plain label, not a URL. Required; exactly one with `--continuous`. Names the cache subdirectory and file `<source>` only when `--name` is omitted. |
| `--name LABEL` | Short source label (e.g. `top`). When given, it **is** the cache subdirectory, the filename `<source>`, and the EXIF/sidecar `camera` field. sage-yolo2 names its crop dirs `<name>-crop-N`. |
| `--from-cache DIR` | One-shot only: upload the newest cached frame from `DIR`. |

### Cache

| Flag | Meaning |
|------|---------|
| `--cache-root DIR` | Ring base directory. Defaults to `$IS2_CACHE_ROOT`, then `/local-cache`. Must exist and be writable. |
| `--cache-name NAME` | Cache instance name, the first directory level (e.g. `camera`). Defaults to the job id. |
| `--cache-max-count N` / `--cache-max-mb MB` | Ring caps. At least one is required with `--continuous`. |
| `--heartbeat-secs S` | Heartbeat interval (default 60), independent of the capture interval. |
| `--max-count N` / `--max-runtime S` | Exit cleanly after N captures or S seconds (0 = never). Useful for bounded test runs or time-shared slots. |

### Camera connection (images and `camera_mic` audio)

| Flag | Meaning |
|------|---------|
| `--camera-host HOST` | Camera IP (default env `CAMERA_HOST`). |
| `--camera-port PORT` | Camera **HTTP** port (default env `CAMERA_PORT`, else 80). Used for both the snapshot API and the FLV audio stream. |
| `--camera-channel N` | Camera channel (default 0). |
| `--capture-timeout S` | Timeout for fetching an image still. |

**Credentials are env-only:** `CAMERA_USER` / `CAMERA_PASSWORD`, passed via
`pluginctl run --env-from <file>`. There's deliberately no flag for them, so a
password never appears in `ps`, shell history, or the pod spec.

### Audio

| Flag | Meaning |
|------|---------|
| `--media {image,audio}` | Media type. Default `image`. |
| `--source-type TYPE` | `camera_mic`, `usb_mic` (default), `rtsp_audio`, `file`, or `camera_still`. Chooses the ffmpeg input and is recorded in the sidecar. |
| `--audio-source SRC` | ALSA device (`hw:1,0`) or a credential-free URL. Not used with `camera_mic`, whose URL is built from `--camera-host` and the env credentials. |
| `--clip-seconds N` | Clip length; defaults to the `--continuous` period, so clips tile the timeline. The ffmpeg timeout is `clip + 15 s`. |
| `--audio-format {flac,wav}` | Default `flac`. |
| `--bandpass-fmax HZ` | Low-pass filter (e.g. `8000` for a 16 kHz camera mic). |

### Node identity and provenance

`--vsn`, `--node-id`, `--lat`, `--lon`, `--job`, `--task`, `--plugin-version`,
`--node-manifest`. Identity is resolved in this order:

1. flags
2. the `WAGGLE_NODE_*` env (from wes-nodeinfo-injection)
3. `/etc/waggle` files
4. the placeholder `NODE`

Pods launched with `pluginctl run` get neither the env nor the files, so pass
`--vsn` (and `--lat`/`--lon` if you want GPS in EXIF). Details:
[docs/HOW-IT-WORKS.md §4](docs/HOW-IT-WORKS.md#4-node-identity-where-vsn-and-gps-come-from).

## Repository layout

| Path | What |
|------|------|
| `*.py` (root) | The plugin. Module map in [docs/HOW-IT-WORKS.md §6](docs/HOW-IT-WORKS.md#6-media-sampler3-code-map). |
| `tests/` | Unit tests (`make test`; no camera or node needed). |
| `Dockerfile`, `Makefile` | Image build (`make sideload` on a node). |
| `sage.yaml`, `ecr-meta/` | Plugin record and ECR catalog metadata. |
| `jobs/` | **Untested** SES job templates for when the image is published. The verified path today is `pluginctl run`. |
| `docs/AUDIO-EXTENSION-DESIGN.md` | Audio design and decisions (still current). |
| `docs/history/` | Design history: stage notes, dated status, the full development log. |

## Provenance and changelog

- Upstream: https://github.com/waggle-sensor/plugin-imagesampler. Forked from the
  enhanced imagesampler ("image-sampler2") at v0.5.1, then generalized to image
  and audio. See [DESIGN-PATH.md](DESIGN-PATH.md).
- Changes: [CHANGELOG.md](CHANGELOG.md). Add an entry under `[Unreleased]` with
  each change; bump `sage.yaml` when a version ships.
- Contact: Pete Beckman, pete.beckman@northwestern.edu
