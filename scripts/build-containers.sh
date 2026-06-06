#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

APT_MIRROR="${APT_MIRROR:-mirrors.tuna.tsinghua.edu.cn}"

IMAGES=(
    "container_py:oj-python:latest"
    "container_java:oj-java:latest"
    "container_cpp:oj-cpp:latest"
    "container_c:oj-c:latest"
    "container_other:oj-other:latest"
)

for entry in "${IMAGES[@]}"; do
    ctx="${entry%%:*}"
    tag="${entry#*:}"
    echo "=== Building $tag from docker/judge/$ctx ==="
    docker build \
        --build-arg "APT_MIRROR=${APT_MIRROR}" \
        -t "$tag" \
        "docker/judge/$ctx"
    echo ""
done

echo "All 5 images built:"
docker images | grep "^oj-" || true
