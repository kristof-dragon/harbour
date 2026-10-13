#!/bin/sh
# Run from any directory. Requires Docker; does not change the host Python.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image=$(sed -n 's/^FROM \(python:[^ ]*\).*/\1/p' "$root/Dockerfile")
test -n "$image"
docker run --rm --user "$(id -u):$(id -g)" \
    -e PIP_CACHE_DIR=/tmp/pip-cache -v "$root:/work" -w /work "$image" sh -eu -c '
    python -m venv /tmp/lock-env
    /tmp/lock-env/bin/python -m pip install --disable-pip-version-check pip==26.2.1 pip-tools==7.6.2
    export CUSTOM_COMPILE_COMMAND="sh scripts/lock_dependencies.sh"
    /tmp/lock-env/bin/pip-compile --cache-dir=/tmp/pip-tools-cache --upgrade --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host --output-file=requirements.txt requirements.in
    /tmp/lock-env/bin/pip-compile --cache-dir=/tmp/pip-tools-cache --upgrade --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host --output-file=requirements-dev.txt requirements-dev.in
    '
