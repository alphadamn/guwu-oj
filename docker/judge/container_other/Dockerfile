FROM debian:bookworm-slim

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && sed -i "s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils \
    rustc \
    golang \
    nodejs \
    npm \
    ruby \
    kotlin \
    time \
    tini \
    && npm install -g @types/node \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_TOOL_OPTIONS="-XX:+IgnoreUnrecognizedVMOptions"
ENV GOCACHE=/sandbox/.cache/go-build
ENV GOPATH=/sandbox/go
ENV PATH=$PATH:/sandbox/go/bin
RUN mkdir -p /sandbox/.cache/go-build

ENTRYPOINT ["/usr/bin/tini", "--"]
WORKDIR /sandbox
