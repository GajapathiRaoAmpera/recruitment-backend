# Judge0 1.13.1 patched to run on cgroup v2 hosts (current Docker / Ubuntu / Debian).
#
# Stock 1.13.1 ships isolate 1.8.1, which only speaks cgroup v1, so every run fails with
# "Failed to create control group /sys/fs/cgroup/memory/box-N/". This image:
#   1. builds isolate 2.x (cgroup v2) and installs it in place of 1.8.1
#   2. drops the two isolate flags 2.x no longer has (--cg-timing / --no-cg-timing)
#   3. sets up the container's cgroup for isolate at start (entrypoint.sh)
#
# Build context is this folder only:  docker build -t <name> infra/judge0

# --- stage 1: build isolate 2.x ------------------------------------------------------------
# The base image is Debian 10 (old gcc and kernel headers, no clone3), too old for isolate 2.x.
# Build on a current Debian and link statically so the binary runs on the old base unchanged.
FROM debian:bookworm-slim AS isolate-build
# v2.7 (2026-08-24). Pinned to a commit and checked against a checksum.
ARG ISOLATE_COMMIT=8f185bb37f3f23e29b33b0c7727c91c13429abe3
ARG ISOLATE_SHA256=00ffa7006e79dac51d1bd1899224cac55c9875445f4926ee083eee938eaefbb9
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev make pkg-config libcap-dev libseccomp-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/isolate.tar.gz "https://github.com/ioi/isolate/archive/${ISOLATE_COMMIT}.tar.gz" \
 && echo "${ISOLATE_SHA256}  /tmp/isolate.tar.gz" | sha256sum -c - \
 && mkdir /tmp/isolate-src \
 && tar -xzf /tmp/isolate.tar.gz --strip-components=1 -C /tmp/isolate-src \
 && make -C /tmp/isolate-src isolate \
      CFLAGS_HARDEN='-D_FORTIFY_SOURCE=3 -fstack-protector-strong -fstack-clash-protection' \
      LDFLAGS_HARDEN='-static -Wl,-z,noexecstack -Wl,-z,relro -Wl,-z,now' \
 && ! ldd /tmp/isolate-src/isolate 2>/dev/null

# --- stage 2: the image we ship ------------------------------------------------------------
FROM judge0/judge0:1.13.1
USER root

COPY --from=isolate-build /tmp/isolate-src/isolate /usr/local/bin/isolate
RUN chown root:root /usr/local/bin/isolate && chmod 4755 /usr/local/bin/isolate
COPY isolate.cf /usr/local/etc/isolate
RUN mkdir -p /run/isolate/locks /var/local/lib/isolate

# Judge0 passes --cg-timing / --no-cg-timing; isolate 2.x always times with the cgroup and
# rejects both. Remove them, and fail the build if any is left or the line moved.
RUN sed -i -E 's/#\{submission\.enable_per_process_and_thread_time_limit \? \(cgroups\.present\? \? "--no-cg-timing" : ""\) : "--cg-timing"\} \\/\\/' /api/app/jobs/isolate_job.rb \
 && ! grep -rn 'cg-timing' /api/app /api/lib /api/config \
 && test "$(grep -c -- '--run' /api/app/jobs/isolate_job.rb)" -ge 3

COPY entrypoint.sh /usr/local/bin/judge0-entrypoint.sh
RUN chmod 755 /usr/local/bin/judge0-entrypoint.sh

USER judge0
ENTRYPOINT ["/usr/local/bin/judge0-entrypoint.sh"]
CMD ["/api/scripts/server"]
