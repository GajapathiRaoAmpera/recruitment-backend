#!/bin/bash
# Prepare the container's cgroup (v2) for isolate, then hand over to Judge0's own entrypoint.
#
# cgroup v2 rule: a cgroup may only delegate controllers to children while it holds no
# processes itself. So move everything into a leaf cgroup, then enable controllers on the root.
# This only works when the container may write to /sys/fs/cgroup (privileged: true, which
# the workers need anyway). The web server container does not run code and skips it.
set -u

CG=/sys/fs/cgroup
if [ -f "$CG/cgroup.controllers" ] && sudo test -w "$CG/cgroup.subtree_control"; then
  sudo bash -s <<'SETUP' || echo "judge0-entrypoint: cgroup setup failed; code runs will fail" >&2
set -e
cd /sys/fs/cgroup
mkdir -p init
for pid in $(cat cgroup.procs); do echo "$pid" > init/cgroup.procs 2>/dev/null || true; done
for c in memory pids cpu; do
  if grep -qw "$c" cgroup.controllers; then echo "+$c" > cgroup.subtree_control; fi
done
SETUP
fi

exec /api/docker-entrypoint.sh "$@"
