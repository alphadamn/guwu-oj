FROM debian:bookworm-slim

# 国内 apt 镜像，构建时可覆盖: --build-arg APT_MIRROR=mirrors.aliyun.com
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && sed -i "s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ \
    python3 \
    openjdk-17-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sandbox
