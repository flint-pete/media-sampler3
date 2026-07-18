# image-sampler2 — CPU-only frame producer/uploader.
#
# Base: python:3.12-slim (same family as the birdnet plugin, which builds cleanly
# on the ECR Jenkins/buildkit builder). We deliberately do NOT use
# waggle/plugin-base:1.1.1-base: it ships Python 3.8 (2019-era) AND its container
# config trips a buildkitd/runc sandbox bug on the ECR builder — every RUN step
# dies at container init with `can't mask dir "/proc/acpi"`. python:3.12-slim
# avoids both problems.
FROM python:3.12-slim

WORKDIR /app

# Python deps only. We use pywaggle's CORE Plugin (waggle.plugin.Plugin) for
# uploads/publishes -- NOT the [vision] extra (no cv2/OpenCV anywhere; the OpenCV
# fallback in acquire.py is a stub, and frames are fetched via stdlib urllib). So
# no OpenCV/numpy and no apt system libs (libGL etc.) are needed -> smaller image,
# faster build, fewer failure surfaces.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# The plugin modules: app + acquire/metadata/nodemeta/upload/capture/cache +
# heartbeat (Stage-5 liveness). Copied AFTER the pip layer so code edits don't
# invalidate the dependency cache.
COPY app.py acquire.py metadata.py nodemeta.py upload.py capture.py cache.py heartbeat.py /app/

ENTRYPOINT ["python3", "-u", "/app/app.py"]
