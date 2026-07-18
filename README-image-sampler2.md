# image-sampler2

An enhanced fork of the Sage/Waggle **imagesampler** plugin
(`waggle-sensor/plugin-imagesampler`, portal: `yonghokim/imagesampler`).

## Status

**v0.5.1 — Stages 0-6 + 3.3 shipped and verified end-to-end on H00F.** 229 tests
passing. Implemented: CLI contract + fail-fast validation (S0), single real
capture (S1), capture-time v2 naming + self-describing EXIF/JSON embed (S2),
one-shot upload to Beehive with placeholder node identity (S3, full round-trip
confirmed), `--continuous` ring cache (S4), cache heartbeat/liveness (S5),
`--from-cache` uploader (S6), and self-exit bounds `--max-count`/`--max-runtime`
for GPU time-sharing (S3.3). 0.5.1 also modernized the container
(`python:3.12-slim`, slimmed deps). Full design:
`docs/imagesampler.flint.analysis.txt`.

The producer/consumer loop uses the shared `/local-cache` node cache provided by
the `wes-local-cache-manager` WES component; if that mount is absent the plugin
**fails fast** rather than writing to an ephemeral path no consumer can read.
Remaining platform gaps: normal ECR deployment is blocked by the build bug, and
real node identity/geotags await the pywaggle/WES runtime calls. See
`readiness-gap.txt`.

> **Platform blockers** (outside this plugin) are tracked with issue-ready
> writeups in `~/AI-projects/sage-design-planning/Infra-problems-to-fix.md` (ECR `/proc/acpi` build
> bug -> filed waggle-edge-stack#110; arm64/Thor build; side-load path; runtime
> GPS/VSN; shared cache mount).
>
> **Future work INSIDE this plugin** (multi-vendor cameras, OpenCV fallback,
> resize/quality decision, from-cache selectors, real-identity wiring, cross-user
> cache perms) is tracked in `~/AI-projects/sage-design-planning/plugin-improvements.md` (IS-1..IS-7)
> and summarized in `readiness-gap.txt`.

## Provenance of the baseline

- Upstream: https://github.com/waggle-sensor/plugin-imagesampler (branch `main`)
- Upstream version at copy time: **0.3.8** (see `sage.yaml`)
- All 12 upstream files copied faithfully (text files sha256-verified against
  upstream; binary `ecr-meta/*.jpg` byte-sizes verified).
- Copied on: 2026-07-03.

## What's added on top of the baseline

- `docs/imagesampler.flint.analysis.txt` — full code study of the upstream
  plugin: camera source, sample frequency, when/how images are saved,
  shortcomings, upgrade options, the pywaggle `{timestamp}-{filename}` naming
  mechanism, upload metadata (what pywaggle vs the server adds), the
  two-timestamp (capture + upload) design, and verified RTSP `sample.timestamp`
  semantics.

## Next steps

See `readiness-gap.txt` for what's left to be usable and
`~/AI-projects/sage-design-planning/plugin-improvements.md` (IS-1..IS-7) for the plugin-side backlog.
The near-term in-our-control items are the cross-user cache permission probe
(once a shared mount exists) and Hanwha SUNAPI camera support (once hardware is
reachable).

## Changelog

All improvements and design changes are recorded in `CHANGELOG.md`. **After each
improvement or design change to the code, add a short entry under `[Unreleased]`.**
When a set of changes is cut into a plugin version, move them under a new version
heading and bump `sage.yaml`.
