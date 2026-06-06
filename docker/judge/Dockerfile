FROM debian:bookworm-slim

# 国内 apt 镜像，构建时可覆盖: --build-arg APT_MIRROR=mirrors.aliyun.com
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && sed -i "s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ \
    gcc \
    binutils \
    python3 \
    openjdk-17-jdk-headless \
    time \
    tini \
    rustc \
    golang \
    procps \
    nodejs \
    npm \
    ruby \
    kotlin \
    && npm install -g typescript ts-node @types/node \
    && rm -rf /var/lib/apt/lists/*

# Suppress Kotlin JVM deprecation warnings
ENV JAVA_TOOL_OPTIONS="-XX:+IgnoreUnrecognizedVMOptions"

# Setup Go cache directory
RUN mkdir -p /sandbox/.cache/go-build
ENV GOCACHE=/sandbox/.cache/go-build
ENV GOPATH=/sandbox/go
ENV PATH=$PATH:/sandbox/go/bin

ENTRYPOINT ["/usr/bin/tini", "--"]

WORKDIR /sandbox
