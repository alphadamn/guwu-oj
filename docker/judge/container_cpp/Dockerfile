FROM debian:bookworm-slim

ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && sed -i "s|security.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ \
    ccache \
    time \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Precompiled header for the C++ standard library. Every submission is
# unique source, so ccache rarely produces a hit, but parsing/template
# instantiation of bits/stdc++.h accounts for ~90% of compile time and is
# identical for all of them. The PCH cuts a cold compile ~3x (and a 4-way
# concurrent build ~3.4x on a weak 4-core judge). Its flags MUST match
# compile_cpp (-std=c++17 -O2) exactly or GCC silently ignores it.
RUN mkdir -p /pch \
    && printf '#include <bits/stdc++.h>\n' > /pch/stdc++.h \
    && g++ -std=c++17 -O2 -x c++-header -o /pch/stdc++.h.gch /pch/stdc++.h \
    && printf '#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <math.h>\n#include <ctype.h>\n#include <time.h>\n#include <assert.h>\n#include <stddef.h>\n#include <stdint.h>\n#include <inttypes.h>\n#include <limits.h>\n#include <float.h>\n#include <errno.h>\n#include <stdbool.h>\n' > /pch/c_headers.h \
    && gcc -O2 -x c-header -o /pch/c_headers.h.gch /pch/c_headers.h

# Mountpoint for the host-wide shared ccache, bind-mounted at run time. It must
# exist in the image because the container root is mounted read-only.
RUN mkdir -p /ccache

ENTRYPOINT ["/usr/bin/tini", "--"]
WORKDIR /sandbox
