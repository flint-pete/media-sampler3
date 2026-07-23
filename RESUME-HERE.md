# RESUME HERE — media-sampler3 audio producer

**One-file pickup point.** Read this to resume cold. Last worked: 2026-07-23.

---

## TL;DR — where things stand

media-sampler3 = a versatile fork of image-sampler2 that captures **audio clips**
(FLAC) into the shared `/local-cache` the same self-describing v2 way it captures
JPEGs. **The audio path is code-complete, reviewed, and ON-NODE VALIDATED on H00F,
including a full sustained-liveness + Beehive-telemetry run.**

- Repo: `~/AI-projects/media-sampler3`, clean, `main`. Version **0.1.0**.
- Tests: **237 passed / 8 skipped** (`make test`; the 8 skips = image tests, no PIL
  in `.venv-test`).
- Image on H00F: built native aarch64, side-loaded into k3s as
  `localhost/media-sampler3:0.1.0` (linux/arm64).
- **DONE:** Steps 0-4. Step 4 (2026-07-23): bounded 5-min production run held the
  ring at cap 20, stayed 1/1 Running the whole window, self-exited clean, and its
  `env.mediasampler.cache.*` heartbeats reached Beehive (queryable, meta vsn=H00F).
- **NEXT:** Step 5 finalize + the persistent-deploy decision. See "NEXT STEPS".

---

## What was validated on H00F (2026-07-19)

Ran via `sudo pluginctl run` against the **live Reolink hummingbird camera mic**
(`10.107.0.221:10000`, `channel0_sub` — the SAME source BirdNet uses in production),
writing to the **real** `/media/plugin-data/local-cache`.

| Behavior | Result |
|----------|--------|
| Live camera-mic capture | valid 16 kHz mono FLAC clips (ffprobe-confirmed) |
| v2 sidecar `<clip>.json` | correct: `media_type=audio`, `source_type=camera_mic`, `source`/`camera` alias, `schema_version=sage-media-1`, real SHA-256 `unique_id` |
| Ring cap + eviction | held at `--cache-max-count`, oldest-first, clip+sidecar atomic, 0 stray `.tmp` |
| Heartbeat | fires on its own `--heartbeat-secs` grid; delta-reset written/evicted counters |
| Bug-fix confirmations (live) | `-f` muxer (clips write `.tmp` then commit), `clip_seconds+grace` timeout, env-only creds (`--env-from`, no password in argv) |
| Cross-user cache READ | root-produced 0644 files read by `beckman` (uid 2016) AND `nobody` (uid 65534) — the actual producer/consumer requirement |

Blockers 1 (`/local-cache` mount) and 2 (cross-user read) in `readiness-gap.txt`
are RESOLVED (both verified live — the mount + wes-local-cache-manager DaemonSet are
deployed and the loop runs in prod: image-sampler2 → yolo2/bioclip2 consumers).

**Known remaining limitation:** `vsn=NODE` placeholder + null GPS. This is the WES
runtime-identity gap (Limiter 1) — identical to every other plugin; Beehive attaches
the real `vsn=H00F` downstream via routing. Not a media-sampler3 bug.

---

## The producer/consumer model (the "why")

One producer per media type bounds its OWN ring; consumers are **non-destructive
readers**. Today in prod on H00F:
- `image-sampler2` (producer) → `/local-cache/hummingcam/top/` (JPEGs)
- `sage-yolo2` (consumer) reads those, writes crops to `hummingcam-crops/`
- `sage-bioclip2` (consumer) reads crops

media-sampler3 adds the audio producer: writes its own `--cache-name` subtree
(e.g. `hummingcam-audio/`) alongside the image cache. A future **BirdNet consumer**
reads those clips — no extra plumbing, because cross-user read is confirmed.

Cross-user *eviction* is owner-gated but by design **never needed** (a consumer
never deletes a producer's files; the producer alone bounds its ring).

---

## How to reach the node

`ssh beckman@node-H00F.sage` (Thor ARM64, passwordless sudo, k3s v1.34).
Camera is live: a snap/audio-pull from `10.107.0.221:10000` works right now.

Build tooling on the node: `docker`/`podman` 4.9.3, `k3s`, `pluginctl`.
Podman tags images `localhost/<name>` — mind the prefix in k3s.

---

## NEXT STEPS

### Step 4 — DONE (2026-07-23) ✓
Bounded 5-min production run validated sustained liveness end-to-end:
pod 1/1 Running 0 restarts for the full window; ring filled 1→20 and held at cap;
`--max-runtime` self-exit clean (exit 0); newest clip ffprobe-valid FLAC; and the
`env.mediasampler.cache.*` heartbeats were queryable from Beehive
(data.sagecontinuum.org) with meta `vsn=H00F`. Details in CHANGELOG [Unreleased].

### Decision waiting for Pete — persistent deploy?  ← DO THIS NEXT
Everything is proven; the only remaining product question is whether to leave a
**genuinely persistent** audio producer running on H00F.
- **Option B — persistent deploy:** submit `jobs/producer-audio-continuous.yaml`
  (edit first: `--source-type camera_mic`, `--camera-host 10.107.0.221`, and wire
  creds via a **k8s Secret / `secretRef`**, NOT inline args — see the
  FUTURE-ENHANCEMENT note in `readiness-gap.txt`). Runs indefinitely as the real
  audio producer feeding a future BirdNet consumer.
- **Or stop here** — the plugin is validated; deploy when a consumer is ready.

To re-run the bounded validation anytime, recreate the creds file (below) and use
the `sudo pluginctl run ... --max-runtime 300` command from the CHANGELOG entry.

Creds file (deleted after each session for hygiene; `sage` account, Pete has the
password — do NOT hardcode into any committed file):
```bash
ssh beckman@node-H00F.sage
umask 077
printf 'CAMERA_USER=sage\nCAMERA_PASSWORD=<REDACTED>\n' > ~/ms3-creds.env
chmod 600 ~/ms3-creds.env
```

### Step 5 — finalize
- Record Step 4 evidence (pod uptime, ring listing, portal heartbeat query) into
  `readiness-gap.txt` / CHANGELOG.
- Clean up test subtrees + creds file on the node:
  ```bash
  sudo rm -rf /media/plugin-data/local-cache/hummingcam-audio
  rm -f ~/ms3-creds.env
  ```
- Only `hummingcam` and `hummingcam-crops` should remain in `/local-cache`.

### Later / optional (tracked in readiness-gap.txt)
- **k8s Secret for camera-mic creds** in the deploy manifest (code already reads
  from env; this is manifest + docs only). FUTURE-ENHANCEMENT.
- **BirdNet audio consumer** wired to read the `hummingcam-audio/` subtree.
- **`--from-cache` audio uploader** job (mirror the image uploader pattern).
- Runtime VSN/GPS wiring once WES exposes the calls (Limiter 1, upstream).
- Pre-publish scrub of the inherited LAN IP `10.107.0.221` (CHANGELOG history note +
  `tests/test_acquire_stage1.py` fixture) — repo is private now; only matters before
  any public push. Real camera-password rotation is Pete's, post-validation.

---

## Cleanup checklist (run at the end of any node session)
```bash
ssh beckman@node-H00F.sage '
  rm -f ~/ms3-creds.env
  sudo rm -rf /media/plugin-data/local-cache/hummingcam-audio*   # test subtrees only
  sudo k3s kubectl get pods -A | grep ms3-audio || echo "no ms3 pods"
  ls /media/plugin-data/local-cache/    # expect only: hummingcam  hummingcam-crops
'
```

## Key files
- `RESUME-HERE.md` (this file) — the pickup point.
- `README.md` — user-facing overview + full flag reference.
- `readiness-gap.txt` — blockers/limiters, now with RESOLVED notes + evidence.
- `jobs/producer-audio-continuous.yaml` — audio producer job manifest (edit for
  camera_mic + Secret before a persistent deploy).
- `docs/AUDIO-EXTENSION-DESIGN.md` — the audio design + locked decisions.
- `CHANGELOG.md` — full history incl. the on-node validation entry.

## Commit trail (this arc)
```
ab08fff docs(readiness): Blocker 2 (cross-user read) RESOLVED
b7d7f8a docs(readiness): Blocker 1 (/local-cache mount) RESOLVED
d5d551f fix(docker): install ffmpeg + ship audio modules (Step 0)
153b5ad fix(audio): force ffmpeg muxer (-f) + independent-review follow-ups
426745d docs: full pass for image+audio producer + audio job manifest
ef0772b fix(audio): code-review fixes — timeout, credential exposure, robustness
a9b59fb feat(audio): wire audio path into app.py continuous loop (TDD)
```
