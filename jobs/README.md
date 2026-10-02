# media-sampler3 example jobs (SES templates)

> **Status: untested SES templates.** The verified way to run media-sampler3 today
> is `sudo pluginctl-nodeinfo run` with a side-loaded image —
> [INSTALLING-MEDIA-SAMPLER3.md](../INSTALLING-MEDIA-SAMPLER3.md) Step 6g and
> [REBOOT-RECOVERY.md](../REBOOT-RECOVERY.md). These files show the intended
> scheduled-job shape once the image is published to the registry. Before
> submitting one: replace `<VSN>`, add the `/local-cache` hostPath volume mount
> your SES version supports, and confirm how it maps a Secret into the pod env.

Job manifests for the producer/consumer split.

| Job | Mode | Role |
|-----|------|------|
| `producer-continuous.yaml` | `--continuous` (image) | Fills a local ring cache with JPEG frames. **Never uploads.** |
| `producer-audio-continuous.yaml` | `--continuous --media audio` | Fills a local ring cache with FLAC clips + sidecars. **Never uploads.** |
| `uploader-from-cache.yaml` | `--one-shot --from-cache` | Uploads the **newest** cached frame on a cron. **Never touches the source.** |

Each producer captures one stream per process (one camera OR one mic); run a
separate job per stream. Edit the placeholders (`CAMERA_IP_HERE`, the ALSA device
`hw:1,0`, `--cache-name`, `--stream`) before submitting.

## Why two jobs instead of one

`--continuous` is strictly **local-only** (it caches, never uploads) and
`--one-shot` always uploads and never caches. `--from-cache` is the bridge that
lets a one-shot uploader publish what the continuous producer cached. Splitting
them means:

- **The upload rate is independent of the capture rate.** Raise the upload cadence
  without adding any camera load — the uploader reads the cache, not the camera.
- **The cache is shareable.** The same ring the uploader reads can be read by
  other consumers (YOLO, BioClip, BirdNet) — one cheap producer, many consumers.
- **The sampler stays a pure producer**, which keeps its role simple and testable.

## Wiring

The uploader's `--from-cache` points at the producer's **stream dir**:

```
<cache-root>/<cache-name>/<source>/   ==   /local-cache/camera/top
```

so the two jobs must agree on `--cache-root`, `--cache-name`, and the source
label (`<source>` is the producer's `--name` if given, otherwise its `--stream`).

## Credentials (producer only)

The producer reads `CAMERA_USER` / `CAMERA_PASSWORD` from the **environment only**
(never args — keeps secrets out of argv / the scheduler record). Provide them via a
Kubernetes Secret mapped into the pod env (`envFrom: secretRef`). Create the Secret
once:

(`camera` here is just an example `<cache-name>` — choose your own)

```
sudo k3s kubectl create secret generic camera-creds \
  --from-literal=CAMERA_USER='<USER>' --from-literal=CAMERA_PASSWORD='<PASS>'
```

The uploader needs **no** credentials — it never contacts the camera.

## Submit

```
sesctl --server https://es.sagecontinuum.org --token "$SES_USER_TOKEN" create -f jobs/producer-continuous.yaml
sesctl ... submit -j <returned-id>
sesctl ... create -f jobs/uploader-from-cache.yaml
sesctl ... submit -j <returned-id>
```

## Notes

- `--cache-root` defaults to `/local-cache`, the shared node cache provided by the
  `wes-local-cache-manager` WES component (mounted into the pod via
  `pluginctl run -v <host>:/local-cache`). If it is not an existing, writable
  directory the plugin **fails fast** with an explanation — there is no silent
  fallback. For off-node local development, pass `--cache-root <dir>` pointing at an
  existing writable directory.
- Empty cache is a **fail-fast** (exit 2) for the uploader: if the producer isn't
  running, the uploader surfaces the misconfig instead of silently doing nothing.
- Adjust the cron in each job's `scienceRules` to tune capture / upload cadence.
