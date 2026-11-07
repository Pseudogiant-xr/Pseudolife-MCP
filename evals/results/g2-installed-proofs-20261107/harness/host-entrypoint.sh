#!/bin/sh
# cgroup v2 nesting, as the official docker:dind entrypoint does: move this
# container's processes into a leaf cgroup and delegate every controller,
# so the inner dockerd can apply memory and CPU limits.
set -e
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  mkdir -p /sys/fs/cgroup/init
  xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs || :
  sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
fi
exec dockerd --host=unix:///var/run/docker.sock
