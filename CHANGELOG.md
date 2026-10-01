# Changelog

All notable changes to **media-sampler3** are recorded here. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
`sage.yaml`.

Convention: add an entry under `[Unreleased]` with each change. When a version
ships, move the entries under a `[x.y.z] - YYYY-MM-DD` heading and bump
`sage.yaml`.

The detailed, dated development log (stage-by-stage build-up, on-node validation
runs, the H00F cut-over, and the inherited image-sampler2 releases 0.2.0–0.5.1) is
preserved in
[docs/history/CHANGELOG-development-log.md](docs/history/CHANGELOG-development-log.md).

## [Unreleased]

## [0.1.0] - 2026-10-01

First tagged release of media-sampler3: the image and audio producer for the
shared `/local-cache`, verified end to end on H041 (Sep 2026).

### Added
- Audio as a second media type (`--media audio`). Bounded FLAC/WAV clips via
  ffmpeg from a camera mic (Reolink HTTP-FLV, `--source-type camera_mic`) or a USB
  mic (ALSA), each with a `<clip>.json` provenance sidecar written before the clip.
- Student-facing docs:
  - `INSTALLING-MEDIA-SAMPLER3.md`: full-stack install
  - `REBOOT-RECOVERY.md`: restart runbook
  - `docs/HOW-IT-WORKS.md`: data flow, identity, code map
  - `DESIGN-PATH.md`: design history index
- `make sideload`: native podman build plus k3s import, with the version taken
  from `sage.yaml`.

### Changed
- Docs reorganized. Current docs are at the top level; dated plans, stage notes,
  status files, spikes, and the development log moved to `docs/history/`.
- Install guide corrections:
  - Audio uses the camera's HTTP port (HTTP-FLV), not RTSP 554.
  - The Tier 2 patch-apply command is fixed.
  - Host Go isn't required.
  - `pluginctl` pods don't receive `wes-identity`.
  - Crop directory naming is explained, along with the known limitations.
- `--name` / `--stream` help text now says what they actually control (the
  `--name` label names the cache subdirectory and the filename source). Log
  prefixes `STAGE n:` were renamed to the mode (`continuous:`, `heartbeat:`, …).
- `sage.yaml`:
  - branch and homepage now point at `master`
  - inputs list all current flags (adds `name`, `media`, the audio flags, and
    `node-manifest`; drops the non-existent `camera-name`)
  - the identity note is corrected
- `jobs/`: marked as untested SES templates; placeholders `<VSN>` and camera
  port 80.
- CI workflow triggers on `master`.

### Security
- Removed a camera-password reference from the history notes, and replaced
  private camera LAN IPs and account names with placeholders across the tree
  (the test fixture now uses the documentation address `192.0.2.10`). `*.env`
  is now gitignored.
