# docs/history — design record (not current instructions)

These files record how media-sampler3 was designed and built: plans, stage notes,
dated status, on-node validation logs, early spikes. They are kept for context and
are **not** maintained. Where they disagree with the current docs
([README](../../README.md), [INSTALLING-MEDIA-SAMPLER3](../../INSTALLING-MEDIA-SAMPLER3.md),
[HOW-IT-WORKS](../HOW-IT-WORKS.md), [REBOOT-RECOVERY](../../REBOOT-RECOVERY.md)),
the current docs win. The narrative overview is [DESIGN-PATH.md](../../DESIGN-PATH.md).

| File | What it is |
|------|-----------|
| `CHANGELOG-development-log.md` | Full dated development log, incl. the inherited image-sampler2 releases 0.2.0–0.5.1, on-node validations, and the 2026-07-23 H00F cut-over |
| `mediasampler.analysis.txt` | Original code study of the upstream imagesampler (the "design §x.y" refs in code comments point here) |
| `IMPLEMENTATION-PLAN.md`, `STAGE*-DESIGN-NOTE.md` | Staged build plan (the "STAGE n" history) |
| `readiness-gap.txt` | July 2026 blocker list (all blockers since resolved) |
| `RESUME-HERE.md` | July 2026 pickup notes for the audio track on H00F |
| `spikes/` | Early throwaway experiments (EXIF embedding, one-shot verification) |
