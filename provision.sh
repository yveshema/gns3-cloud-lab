#!/usr/bin/env bash
#
# provision.sh — GCE startup-script for the gns3-2620-lab VM.
#
# Runs as root, on every boot (GCE re-runs the startup-script metadata key on
# every boot, not just the first), before any student user account exists —
# see CLAUDE.md. Everything user-specific (group membership, GNS3 client
# config, launching the server) happens later in the wrapper's `--start`,
# not here.
#
# Idempotent via a sentinel file: the expensive build/install steps are
# skipped on a second boot, but the assertions always run, so a corrupted
# install is still caught rather than silently passing because the sentinel
# was present.
#
# Design and the empirical findings behind it: CLAUDE.md and
# /workspaces/gns3-cloud-plan.md. In particular:
#   - apt fails atomically: a single `apt install a b c` aborts entirely if
#     any package has no candidate, naming only one of them. Packages here
#     are installed and verified one at a time (apt_install_one) instead.
#   - VPCS must be built from source at tag v0.6.2 (default branch 0.8.4 is
#     rejected by gns3-server; Ubuntu's packaged 0.5b2 is below the floor),
#     with two independent, both-required workarounds for GCC 14 / GCC 10.
#   - uBridge isn't packaged; built from GitHub master, capabilities set
#     directly so no `ubridge` group is needed.
#
# Log: journalctl -u google-startup-scripts.service

set -euo pipefail

SENTINEL_DIR=/var/lib/gns3-2620-lab
SENTINEL="$SENTINEL_DIR/provisioned"
VPCS_TAG=v0.6.2
VPCS_VERSION=0.6.2
GNS3_SERVER_VERSION=2.2.61
SRC_DIR=/usr/local/src

log() {
    echo "[provision] $*"
}

fatal() {
    echo "[provision] FATAL: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# apt: install and verify one package at a time, never a grouped call, so a
# single missing candidate can't silently take out the rest of the list and
# the failure names the actual package that's missing.
# ---------------------------------------------------------------------------
apt_install_one() {
    local pkg="$1"

    if dpkg -s "$pkg" >/dev/null 2>&1; then
        return 0
    fi

    apt-cache show "$pkg" >/dev/null 2>&1 \
        || fatal "apt has no candidate for '$pkg'"

    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$pkg"

    dpkg -s "$pkg" >/dev/null 2>&1 \
        || fatal "'$pkg' reported success but is not installed"
}

install_packages() {
    log "apt-get update"
    apt-get update

    # qemu-kvm is not a real package on Ubuntu 24.04 — it exists only as a
    # virtual name that qemu-system-x86 Provides. `apt-get install qemu-kvm`
    # resolves and installs qemu-system-x86 fine (exit 0), but `dpkg -s
    # qemu-kvm` then fails since no package by that literal name was ever
    # installed, tripping apt_install_one's own post-install check. Request
    # the real package name instead of the alias. Confirmed on a live VM.
    local pkg
    for pkg in qemu-system-x86 dynamips docker.io git build-essential libpcap-dev pipx; do
        log "installing $pkg"
        apt_install_one "$pkg"
    done

    systemctl enable --now docker
}

# ---------------------------------------------------------------------------
# uBridge — build from GitHub master, no packaged version exists.
# ---------------------------------------------------------------------------
build_ubridge() {
    log "building uBridge from source"
    rm -rf "$SRC_DIR/ubridge"
    git clone --depth 1 https://github.com/GNS3/ubridge.git "$SRC_DIR/ubridge"
    make -C "$SRC_DIR/ubridge"
    install -m 755 "$SRC_DIR/ubridge/ubridge" /usr/local/bin/ubridge
    setcap cap_net_admin,cap_net_raw=ep /usr/local/bin/ubridge
}

# ---------------------------------------------------------------------------
# VPCS — must be v0.6.2, not the default branch (0.8.4 is rejected by
# gns3-server's gate). Two build-era workarounds, both required:
#   1. truncate src/getopt.h — GCC 14 errors on its non-const `getopt`
#      declaration; glibc's own declaration in unistd.h is sufficient, and
#      no getopt.c is compiled by Makefile.linux.
#   2. build with `gcc -fcommon` — GCC 10 defaults to -fno-common, which
#      breaks the global `vpc` declared in a header (multiple definitions).
# ---------------------------------------------------------------------------
build_vpcs() {
    log "building VPCS $VPCS_TAG from source"
    rm -rf "$SRC_DIR/vpcs"
    git clone https://github.com/GNS3/vpcs.git "$SRC_DIR/vpcs"
    git -C "$SRC_DIR/vpcs" checkout "$VPCS_TAG"

    : > "$SRC_DIR/vpcs/src/getopt.h"
    make -C "$SRC_DIR/vpcs/src" -f Makefile.linux CC="gcc -fcommon"
    install -m 755 "$SRC_DIR/vpcs/src/vpcs" /usr/local/bin/vpcs
}

# ---------------------------------------------------------------------------
# gns3-server — pipx-managed but installed system-wide (PIPX_HOME /
# PIPX_BIN_DIR), not per-user, so it's on PATH regardless of which student
# account ends up running it.
# ---------------------------------------------------------------------------
install_gns3_server() {
    log "installing gns3-server==$GNS3_SERVER_VERSION via pipx"
    PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
        pipx install "gns3-server==$GNS3_SERVER_VERSION"
}

# ---------------------------------------------------------------------------
# Assertions — one per version/capability gate established in CLAUDE.md.
# Exit non-zero on the first failure so a broken provision is visible in
# journalctl instead of surfacing days later as a node that won't start.
# ---------------------------------------------------------------------------
assert_vpcs() {
    vpcs -v 2>&1 | grep -Fq "$VPCS_VERSION" \
        || fatal "vpcs -v does not report $VPCS_VERSION"
    log "OK: vpcs is $VPCS_VERSION"
}

assert_dynamips() {
    # dynamips doesn't recognize --version as a real flag: it prints the
    # version banner followed by the full usage/help text and exits 1.
    # Under `set -o pipefail` that exit code would propagate as the whole
    # pipeline's status and kill the script via set -e before the checks
    # below ever run — `|| true` defers to those checks instead of trusting
    # dynamips's own exit code. Confirmed on a live VM.
    local version
    version=$(dynamips --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) || true
    [ -n "$version" ] || fatal "could not parse dynamips --version output"
    printf '%s\n%s\n' "0.2.11" "$version" | sort -C -V \
        || fatal "dynamips $version is below the required floor 0.2.11"
    log "OK: dynamips is $version (>= 0.2.11)"
}

assert_kvm() {
    [ -e /dev/kvm ] || fatal "/dev/kvm does not exist — nested virtualization not active"
    log "OK: /dev/kvm present"
}

assert_ubridge() {
    getcap /usr/local/bin/ubridge 2>/dev/null | grep -q "cap_net_admin" \
        || fatal "ubridge is missing cap_net_admin/cap_net_raw capabilities"
    log "OK: ubridge has required capabilities"
}

run_assertions() {
    assert_vpcs
    assert_dynamips
    assert_kvm
    assert_ubridge
}

main() {
    if [ -f "$SENTINEL" ]; then
        log "sentinel present ($SENTINEL) — skipping install, running assertions only"
        run_assertions
        log "provision check complete"
        return 0
    fi

    install_packages
    build_ubridge
    build_vpcs
    install_gns3_server

    run_assertions

    mkdir -p "$SENTINEL_DIR"
    date -u +%FT%TZ > "$SENTINEL"
    log "provision complete, sentinel written"
}

main "$@"
