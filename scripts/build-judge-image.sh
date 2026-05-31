#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# apt 镜像（国内加速）: mirrors.tuna.tsinghua.edu.cn | mirrors.aliyun.com | mirrors.ustc.edu.cn
APT_MIRROR="${APT_MIRROR:-mirrors.tuna.tsinghua.edu.cn}"

docker build \
  --build-arg "APT_MIRROR=${APT_MIRROR}" \
  -t oj-judge:latest \
  docker/judge

echo "Built image: oj-judge:latest (apt mirror: ${APT_MIRROR})"
