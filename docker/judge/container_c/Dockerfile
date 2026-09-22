FROM debian:bookworm-slim

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && sed -i "s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    ccache \
    time \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Mountpoint for the host-wide shared ccache, bind-mounted at run time. It must
# exist in the image because the container root is mounted read-only.
RUN mkdir -p /ccache

ENTRYPOINT ["/usr/bin/tini", "--"]
WORKDIR /sandbox
