#!/usr/bin/env bash
# Build the golden macOS disk that macos-bundles.yml's `run-macos-x64-dockur` runs on.
#
# Issue #277 phase B2. This runs on YOUR machine, not in CI, and that is the point.
#
# Why not in CI: macOS has no unattended installer (the dockur image exposes no automation
# hook, and Apple's Recovery installer is a GUI), so a human has to drive it once. Doing
# that on a GitHub runner means exposing dockur's web viewer -- an unauthenticated noVNC
# console with keyboard and mouse -- to the internet for the hours the install takes, on a
# PUBLIC repository. There is no version of that which is safe enough to be worth it. So
# the install happens here, behind your own loopback, and CI only ever consumes the
# finished image (see .github/workflows/macos-golden-upload.yml).
#
#   ci/macos_golden_local.sh boot     # start the guest, print the click-by-click list
#   ci/macos_golden_local.sh check    # is the guest reachable over ssh yet?
#   ci/macos_golden_local.sh pack     # shut down, compress, checksum, size-gate
#
# Host requirements: Linux x86_64, Docker engine (not Desktop), /dev/kvm reachable from a
# container, ~80 GB free. Verified on an AMD Zen 2 host; see CPU_MODEL below.
set -euo pipefail

CONTAINER="${CONTAINER:-macos-golden}"
STORE="${STORE:-$PWD/macstore}"
OUT="${OUT:-$PWD/macstore.tar.zst}"
# GitHub gives a repository 10 GB of Actions cache IN TOTAL, shared with the soldr caches.
# A golden image that eats all of it would LRU-evict them on every run.
MAX_BYTES="${MAX_BYTES:-9000000000}"

die() { echo "macos_golden_local: $*" >&2; exit 1; }

cmd_boot() {
  command -v docker >/dev/null || die "docker is not on PATH"
  docker run --rm --device=/dev/kvm alpine ls /dev/kvm >/dev/null 2>&1 \
    || die "/dev/kvm is not reachable from a container; this cannot work without KVM"
  mkdir -p "$STORE"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # Ports bound to 127.0.0.1 explicitly. Docker's default (0.0.0.0) would publish an
  # unauthenticated VNC console with keyboard and mouse to every host on your network.
  #
  # CPU_MODEL is NOT dockur's default and it is the difference between working and not.
  # Measured on AMD Zen 2 (Ryzen 7 3700X): with dockur's default `Haswell-noTSX` for
  # macOS 13 the guest resets immediately after "HANDOFF TO XNU" and loops forever (987
  # resets in 7 h, 685 MB ever written, no installer screen). With `Skylake-Client-v4` it
  # reaches Recovery in about 5 minutes. Same result on GitHub's AMD EPYC runners:
  # 33 handoffs and no boot by default, exactly 1 handoff and Recovery with this model.
  # dockur#268 covers the AMD single-core mitigation, which is the CPU_CORES=1 below.
  docker run -d --name "$CONTAINER" \
    --device=/dev/kvm --device=/dev/net/tun --cap-add NET_ADMIN \
    -p 127.0.0.1:8006:8006 -p 127.0.0.1:2222:22 \
    -e VERSION=13 -e RAM_SIZE=8G -e CPU_CORES=1 -e DISK_SIZE=64G \
    -e CPU_MODEL=Skylake-Client-v4 \
    -v "$STORE:/storage" \
    dockurr/macos
  cat <<'EOF'

Guest starting. Open http://127.0.0.1:8006 -- Recovery appears in a few minutes.

Do exactly this, and nothing else:

  1. Disk Utility -> select the largest disk -> Erase -> format APFS -> name it "macOS"
     -> quit Disk Utility.
  2. "Reinstall macOS Ventura" -> target that disk. This is the long part (hours on one
     emulated core). It reboots itself several times; that is normal.
  3. In Setup Assistant create the account:  username `ci`, password `ci`.
     Those exact values -- macos-bundles.yml logs in with them. The guest is ephemeral,
     loopback-only, and holds nothing but test binaries, which is why a fixed local
     password is used instead of a repository secret.
  4. System Settings -> General -> Sharing -> enable Remote Login (ssh).
  5. Terminal:
       sudo systemsetup -setremotelogin on
       sudo pmset -a sleep 0 disksleep 0 displaysleep 0
     A guest that falls asleep mid-run is indistinguishable from a hung one.

  Do NOT install Xcode or the Command Line Tools. The job ships its own relocatable
  python-build-standalone interpreter, and every GB here is a GB of CI cache on every
  future run.

Then:  ci/macos_golden_local.sh check     (until it says READY)
       ci/macos_golden_local.sh pack

EOF
}

cmd_check() {
  command -v sshpass >/dev/null || die "sshpass is not installed"
  if sshpass -p ci ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o ConnectTimeout=5 -o ConnectionAttempts=1 ci@127.0.0.1 true 2>/dev/null; then
    echo "READY -- the guest accepts ssh."
    sshpass -p ci ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      ci@127.0.0.1 'sw_vers; uname -m'
    return 0
  fi
  echo "not ready yet (no ssh on 127.0.0.1:2222)."
  # A slow boot and a panic loop look identical from outside; the handoff count is what
  # tells them apart. A climbing number is a loop.
  echo "XNU handoffs so far (climbing == panic loop, flat == booted or installing): \
$(docker logs "$CONTAINER" 2>&1 | grep -c 'HANDOFF TO XNU' || true)"
  return 1
}

cmd_pack() {
  # `sudo shutdown` over a non-tty ssh session cannot prompt, so feed the password in.
  sshpass -p ci ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    ci@127.0.0.1 'echo ci | sudo -S shutdown -h now' 2>/dev/null || true
  echo "waiting for the guest to power off..."
  for _ in $(seq 1 30); do
    sleep 5
    docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || break
  done
  docker stop -t 120 "$CONTAINER" >/dev/null 2>&1 || true

  echo "store: apparent=$(du -sh --apparent-size "$STORE" | cut -f1) actual=$(du -sh "$STORE" | cut -f1)"
  # -S is load-bearing, and it must be on the CREATE side. The guest disk is a raw image
  # that is mostly holes: 64 GB apparent, a fraction of that allocated. Without -S, GNU
  # tar reads and stores every hole as literal zero bytes -- zstd still compresses them to
  # almost nothing, so the archive looks fine, but EXTRACTION then writes them as real
  # blocks. Measured on a 2 GB/21 MB sparse file: extracting a non-sparse archive costs
  # 2.1 GB on disk, and passing -S at extraction time does NOT help, because the
  # sparseness has to be recorded at creation time. Scaled to a 64 GB disk that exhausts
  # the CI runner, which has ~87 GB free in total.
  tar -C "$STORE" -cSf - . | zstd -T0 -19 -o "$OUT" -f
  size=$(stat -c%s "$OUT")
  echo "packed: $OUT  $(numfmt --to=iec "$size")"
  sha256sum "$OUT"
  if [ "$size" -gt "$MAX_BYTES" ]; then
    echo >&2
    die "image is $(numfmt --to=iec "$size"), over the $(numfmt --to=iec "$MAX_BYTES") budget.
GitHub gives a repository 10 GB of Actions cache in total, shared with the soldr caches,
so an image this size would evict them continuously. Options, in the order worth trying:
reinstall without optional macOS components; shrink DISK_SIZE and redo; or take the
decision to a self-hosted runner (see docs/ci-gates.md)."
  fi
  cat <<EOF

Next: upload $OUT somewhere private that CI can fetch with a URL (a signed object-store
link, for instance -- NOT a public release asset, which would be redistributing macOS),
then run the 'macos-golden-upload' workflow with that URL and the sha256 above. Both are
masked inputs.
EOF
}

case "${1:-}" in
  boot) cmd_boot ;;
  check) cmd_check ;;
  pack) cmd_pack ;;
  *) echo "usage: $0 {boot|check|pack}" >&2; exit 2 ;;
esac
