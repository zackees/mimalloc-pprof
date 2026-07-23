#!/usr/bin/env bash
# managed-by: clud (docker_build_soldr.py)
# Idle entry script — the tool execs `docker run` directly for one-shot
# commands; this script is here so `clud tool run docker/docker_build_soldr.py up`
# has a long-running PID inside the container to attach against.
set -euo pipefail
exec tail -f /dev/null
