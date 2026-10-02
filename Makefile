# media-sampler3 build/test helpers.
#
#   make test       run the unit suite (no camera/node needed)
#   make sideload   ON A THOR NODE: native podman build + import into k3s
#                   containerd as localhost/media-sampler3:<version>
#                   (same as INSTALLING-MEDIA-SAMPLER3.md Step 4)
#   make image      local docker buildx build (dev convenience)
#
# The version comes from sage.yaml, so the tag never drifts from the plugin record.

VERSION?=$(shell sed -n 's/^version: *"\(.*\)"/\1/p' sage.yaml)
IMAGE?=localhost/media-sampler3
PY?=.venv-test/bin/python

all: test

# A fresh clone has no venv: create one with the runtime deps + pytest.
# (PIL is deliberately not installed; the 8 PIL-only image tests skip.)
$(PY):
	python3 -m venv .venv-test
	.venv-test/bin/pip install -q -r requirements.txt pytest

test: $(PY)
	$(PY) -m pytest -q

sideload:
	sudo podman build -t "$(IMAGE):$(VERSION)" .
	sudo podman save "$(IMAGE):$(VERSION)" | sudo k3s ctr images import -
	sudo k3s ctr images ls | grep "media-sampler3:$(VERSION)"

image:
	docker buildx build -t "$(IMAGE):$(VERSION)" --load .

.PHONY: all test sideload image
