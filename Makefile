RELEASE?=0.0.0
PLATFORM?=linux/amd64,linux/arm64
IMAGE=image-sampler
PY?=.venv-test/bin/python

all: image

image:
	docker buildx build -t "waggle/plugin-$(IMAGE):$(RELEASE)" --load .

push:
	docker buildx build -t "waggle/plugin-$(IMAGE):$(RELEASE)" --platform "$(PLATFORM)" --push .

# Run the unit suite in the local test venv (canonical verification command).
test:
	$(PY) -m pytest -q

.PHONY: all image push test
