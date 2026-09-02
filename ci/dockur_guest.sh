#!/usr/bin/env bash
# Talk to the macOS guest that macos-bundles.yml's `run-macos-x64-dockur` boots.
#
# Issue #277 phase B2. This exists so the ssh incantation lives in exactly one place: the
# job runs a dozen commands in the guest, and an option that drifts between them (a
# missing -o UserKnownHostsFile, a forgotten port) fails in a way that looks like a guest
# problem rather than a transport one.
#
#   ci/dockur_guest.sh run  '<shell command>'     run it in the guest, inherit its status
#   ci/dockur_guest.sh pull '<remote glob>' <dst> copy results back out
#   ci/dockur_guest.sh push <local> '<remote>'    copy something in
#
# Configuration comes from the environment the job already sets: GUEST_USER, GUEST_PASS,
# and GUEST_PORT (default 2222).
#
# StrictHostKeyChecking=no + UserKnownHostsFile=/dev/null are correct here and not a
# weakening: the guest is created fresh from a disk image on this runner, reached over
# loopback, and has a different host key every run, so there is no key to pin and nothing
# in the trust store to protect.
set -euo pipefail

: "${GUEST_USER:=ci}"
: "${GUEST_PASS:=ci}"
: "${GUEST_PORT:=2222}"

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=20
)

usage() {
  echo "usage: $0 {run <command> | pull <remote> <local> | push <local> <remote>}" >&2
  exit 2
}

[ $# -ge 1 ] || usage
action="$1"; shift

case "$action" in
  run)
    [ $# -eq 1 ] || usage
    # `bash -lc` so the guest's login PATH applies, and the remote status is this
    # script's status -- the whole point is that a failing test fails the CI step.
    exec sshpass -p "$GUEST_PASS" ssh -p "$GUEST_PORT" "${SSH_OPTS[@]}" \
      "$GUEST_USER@127.0.0.1" "bash -lc $(printf '%q' "$1")"
    ;;
  pull)
    [ $# -eq 2 ] || usage
    exec sshpass -p "$GUEST_PASS" scp -P "$GUEST_PORT" "${SSH_OPTS[@]}" \
      "$GUEST_USER@127.0.0.1:$1" "$2"
    ;;
  push)
    [ $# -eq 2 ] || usage
    exec sshpass -p "$GUEST_PASS" scp -P "$GUEST_PORT" "${SSH_OPTS[@]}" \
      "$1" "$GUEST_USER@127.0.0.1:$2"
    ;;
  *) usage ;;
esac
