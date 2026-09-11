#!/usr/bin/env bash
#
# provision.sh — GCE startup-script for the gns3-cloud-lab VM.
#
# Author: Yves R. Shema <yshema@bcit.ca>
# Co-Authored-By: Claude <noreply@anthropic.com>
#
# Runs as root, on every boot (GCE re-runs the startup-script metadata key
# on every boot, not just the first), before any user account exists —
# everything user-specific (group membership, GNS3 client config, launching
# the server) happens later in the wrapper's `--start`, not here.
#
# Idempotent via a sentinel file: the expensive build/install steps are
# skipped on a second boot, but the libvirt network setup, the firewall
# report and the assertions always run, so a corrupted install is still
# caught rather than silently passing because the sentinel was present.
#
# Every step is also idempotent in its own right, independent of the
# sentinel: `upgrade` clears the sentinel to force a full re-run over an
# already-provisioned machine, and that re-run must not fail just because
# things are already there.
#
# Usage: provision.sh [--dry-run]
#   --dry-run prints every mutating command instead of running it, and
#   simulates the apt work with `apt-get install -s`. GCE runs startup
#   scripts with no arguments, so the flag only ever arrives when a human
#   (or `upgrade --dry-run`) runs the script over SSH.
#
# Log: journalctl -u google-startup-scripts.service

set -euo pipefail

SENTINEL_DIR=/var/lib/gns3-cloud-lab
SENTINEL="$SENTINEL_DIR/provisioned"
VPCS_TAG=v0.6.2
VPCS_VERSION=0.6.2
# uBridge is pinned like VPCS. It used to be a `--depth 1` clone of master,
# which meant the version installed depended on the day the script ran:
# upstream is already past this tag (v1.2.2 exists), so a `create` today
# would have installed something no part of this repo has been validated
# against, and something different from what a student provisioned last
# week is running. v1.2.1 is what gns3-lab runs and what everything here
# was checked against, so it stays the reference until there's a reason to
# move it.
UBRIDGE_TAG=v1.2.1
UBRIDGE_VERSION=1.2.1
GNS3_SERVER_VERSION=2.2.61
SRC_DIR=/usr/local/src
PIPX_ROOT=/opt/pipx
LOCAL_BIN=/usr/local/bin

# libvirt's default NAT network — the one a GNS3 NAT node attaches to.
LIBVIRT_NET=default
LIBVIRT_NET_XML=/usr/share/libvirt/networks/default.xml
LIBVIRT_NET_ADDR=192.168.122.1/24
# Overridable so the unit tests don't have to sit through the real wait.
LIBVIRT_NET_RETRIES="${LIBVIRT_NET_RETRIES:-10}"
LIBVIRT_NET_RETRY_SLEEP="${LIBVIRT_NET_RETRY_SLEEP:-2}"

# Root's default libvirt URI is already qemu:///system, but this script
# runs in whatever environment GCE hands it — naming the URI explicitly
# means the connection can't depend on LIBVIRT_DEFAULT_URI being unset.
VIRSH=(virsh -c qemu:///system)

DRY_RUN=0

log() {
    echo "[provision] $*"
}

fatal() {
    echo "[provision] FATAL: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Every command that changes the machine goes through run(). Under
# --dry-run it prints what it would have done and returns success, so the
# rest of the script's control flow (which guard skipped what, in what
# order) is exercised for real. Read-only probes deliberately do NOT go
# through run(): a dry run that can't see the current state can't report
# which guards would fire.
# ---------------------------------------------------------------------------
run() {
    if [ "$DRY_RUN" = 1 ]; then
        log "DRY-RUN would run: $*"
        return 0
    fi
    "$@"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            *)
                fatal "unknown argument: $1 (usage: provision.sh [--dry-run])"
                ;;
        esac
    done
    if [ "$DRY_RUN" = 1 ]; then
        log "DRY-RUN: nothing on this machine will be changed"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Report the version a locally installed binary claims, or nothing at all
# if it isn't there or won't say. Never fails: a caller that gets no answer
# treats it as "not the version we want" and (re)installs, so an
# unparseable banner costs a rebuild, never a wrong skip.
# ---------------------------------------------------------------------------
binary_version() {
    local path="$1"
    local flag="${2:--v}"

    [ -x "$path" ] || return 0
    "$path" "$flag" 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true
}

# ---------------------------------------------------------------------------
# apt: install and verify one package at a time, never a grouped call, so a
# single missing candidate can't silently take out the rest of the list and
# the failure names the actual package that's missing.
# ---------------------------------------------------------------------------
apt_install_one() {
    local pkg="$1"

    if dpkg -s "$pkg" >/dev/null 2>&1; then
        log "$pkg already installed"
        return 0
    fi

    apt-cache show "$pkg" >/dev/null 2>&1 \
        || fatal "apt has no candidate for '$pkg'"

    if [ "$DRY_RUN" = 1 ]; then
        # -s is apt's own dependency resolver run for real against the real
        # package lists, printing the transaction it would perform. It is a
        # simulation rather than a guess about what apt would do — the one
        # caveat being that the `apt-get update` above was itself skipped,
        # so it resolves against whatever lists the machine already has.
        log "DRY-RUN simulating install of $pkg"
        DEBIAN_FRONTEND=noninteractive apt-get install -s -y --no-install-recommends "$pkg" \
            || fatal "apt-get -s says '$pkg' cannot be installed"
        return 0
    fi

    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$pkg"

    dpkg -s "$pkg" >/dev/null 2>&1 \
        || fatal "'$pkg' reported success but is not installed"
}

install_packages() {
    log "apt-get update"
    run apt-get update

    # qemu-kvm is not a real package on Ubuntu 24.04 — it exists only as a
    # virtual name that qemu-system-x86 Provides. `apt-get install qemu-kvm`
    # resolves and installs qemu-system-x86 fine (exit 0), but `dpkg -s
    # qemu-kvm` then fails since no package by that literal name was ever
    # installed, tripping apt_install_one's own post-install check. Request
    # the real package name instead of the alias.
    #
    # The libvirt block is what makes a GNS3 NAT node work: the NAT node is
    # a front end for libvirt's `default` network (virbr0, 192.168.122.0/24),
    # and none of libvirt was installed here before, so the node had nothing
    # to attach to. Order inside the block matters:
    #
    #   dnsmasq-base BEFORE libvirt-daemon-system. dnsmasq-base is only a
    #   Recommends of libvirt-daemon-system, and apt_install_one installs
    #   with --no-install-recommends, so installing libvirt alone yields a
    #   `default` network that cannot start (libvirt needs dnsmasq to serve
    #   DHCP/DNS on virbr0). That failure looks identical to libvirt not
    #   being installed at all, from an entirely different cause. Installing
    #   it first lets libvirt's own postinst start the network.
    #
    #   iptables by name, even though it is already on the machine as an
    #   automatic dependency of docker.io, and even though libvirt's
    #   `Depends: iptables | firewalld` is already satisfied by it. Naming
    #   it here is about apt's manual/auto flag, not about installing it:
    #   see the apt-mark below.
    #
    #   qemu-utils, for the same reason dnsmasq-base is named above: it's
    #   only a Recommends of qemu-system-x86 (confirmed against Ubuntu
    #   24.04's real package metadata), so --no-install-recommends leaves it
    #   out. It provides qemu-img, which gns3server requires alongside
    #   qemu-system-x86_64 for every QEMU node — without it, GNS3 fails a
    #   node with "Could not find qemu-img in <dir>", naming whatever
    #   directory the configured qemu binary lives in regardless of which
    #   directory that is, which reads like a path-configuration problem
    #   rather than a missing package. See assert_qemu_img below.
    local pkg
    for pkg in qemu-system-x86 qemu-utils dynamips docker.io git build-essential libpcap-dev pipx \
               dnsmasq-base libvirt-daemon-system libvirt-clients iptables; do
        log "installing $pkg"
        apt_install_one "$pkg"
    done

    # apt_install_one returns early when dpkg already knows the package, so
    # on a machine where docker.io pulled iptables in first, the loop above
    # never runs an install for it and its auto-installed flag is never
    # cleared. Removing Docker some months later would then let autoremove
    # take iptables with it, and libvirt networks would stop starting with
    # nothing on the machine to explain why. Marking it manual is what
    # actually pins it.
    run apt-mark manual iptables

    run systemctl enable --now docker
}

# ---------------------------------------------------------------------------
# libvirt: the `default` NAT network must be up on every boot, not just the
# boot that provisioned the machine, so this runs outside the sentinel gate.
# ---------------------------------------------------------------------------
libvirt_network_unit() {
    # Ubuntu 24.04's libvirt (10.0.0) is still the monolithic libvirtd;
    # newer libvirt splits the network driver out into virtnetworkd. Probe
    # for the unit that exists rather than assuming either layout.
    local unit
    for unit in virtnetworkd.service libvirtd.service; do
        if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .; then
            echo "$unit"
            return 0
        fi
    done
    return 1
}

libvirt_net_field() {
    # `virsh net-info default` prints "Active:         yes" etc.; this
    # returns the value for the field named in $1 ("Active:"), or nothing
    # if the network doesn't exist.
    "${VIRSH[@]}" net-info "$LIBVIRT_NET" 2>/dev/null \
        | awk -v field="$1" '$1 == field { print $2 }' || true
}

ensure_libvirt_network() {
    if ! command -v virsh >/dev/null 2>&1; then
        if [ "$DRY_RUN" = 1 ]; then
            log "DRY-RUN: virsh not present (libvirt-clients would have been installed above)" \
                "— skipping network setup"
            return 0
        fi
        fatal "libvirt is not installed on this VM. A VM provisioned before libvirt was"\
              "added here keeps its sentinel and skips the install step, so this is"\
              "expected there — run 'gns3-cloud-lab upgrade' to re-provision it."
    fi

    local unit
    unit=$(libvirt_network_unit) \
        || fatal "neither virtnetworkd.service nor libvirtd.service exists — libvirt is not installed"
    log "enabling $unit"
    run systemctl enable --now "$unit"

    # libvirt-daemon-config-network is a hard dependency of
    # libvirt-daemon-system and it both ships and autostarts the `default`
    # network, so on a normal Ubuntu install the network is already defined
    # by the time this runs. Defining it from the shipped XML is the
    # fallback for a machine where it was removed or never landed.
    if [ -n "$(libvirt_net_field Name:)" ]; then
        log "libvirt network '$LIBVIRT_NET' is already defined"
    elif [ -f "$LIBVIRT_NET_XML" ]; then
        log "libvirt network '$LIBVIRT_NET' is not defined — defining it from $LIBVIRT_NET_XML"
        run "${VIRSH[@]}" net-define "$LIBVIRT_NET_XML"
    elif [ "$DRY_RUN" = 1 ]; then
        log "DRY-RUN: '$LIBVIRT_NET' is not defined and $LIBVIRT_NET_XML is absent" \
            "(libvirt-daemon-config-network would have installed it above)"
        return 0
    else
        fatal "libvirt network '$LIBVIRT_NET' is not defined and $LIBVIRT_NET_XML does not exist"
    fi

    if [ "$(libvirt_net_field Autostart:)" = "yes" ]; then
        log "libvirt network '$LIBVIRT_NET' is already set to autostart"
    else
        log "marking libvirt network '$LIBVIRT_NET' autostart"
        run "${VIRSH[@]}" net-autostart "$LIBVIRT_NET"
    fi

    if [ "$(libvirt_net_field Active:)" = "yes" ]; then
        log "libvirt network '$LIBVIRT_NET' is already active"
    else
        log "starting libvirt network '$LIBVIRT_NET'"
        run "${VIRSH[@]}" net-start "$LIBVIRT_NET"
    fi
}

# ---------------------------------------------------------------------------
# Firewall state — logged, never fatal. This is insurance for the one thing
# the NAT node depends on that this script does not control.
#
# Docker sets `-P FORWARD DROP` and installs DOCKER-USER / DOCKER-FORWARD,
# which on some distributions blocks libvirt guest egress. On Ubuntu 24.04
# it does not, and no workaround is needed: the FORWARD chain jumps to
# libvirt's own chains first —
#     -A FORWARD -j LIBVIRT_FWX / LIBVIRT_FWI / LIBVIRT_FWO
#     -A FORWARD -j DOCKER-USER
#     -A FORWARD -j DOCKER-FORWARD
# — and LIBVIRT_FWO/LIBVIRT_FWI ACCEPT the guest subnet's traffic. ACCEPT in
# a user-defined chain is a terminating verdict, so that traffic never
# reaches Docker's chains and the DROP policy never applies to it.
# (Confirmed live on 2026-09-04. A Fedora workstation behaves differently
# because its libvirt 12 uses the nftables backend instead.)
#
# That ordering is not something this script sets, so if a future Docker or
# libvirt package reorders it, the breakage would surface weeks later as
# "the NAT node can't reach the internet". Printing the evidence into the
# journal on every boot means the answer is already sitting in the log.
# ---------------------------------------------------------------------------
report_firewall_state() {
    log "--- firewall state (informational, never fatal) ---"
    # Plain `virsh --version` (no -c): this is the client version and must
    # not depend on the daemon answering a connection. Stderr is dropped
    # rather than logged so an absent virsh reports as "unknown" instead of
    # pasting a shell "command not found" into the middle of the report.
    local libvirt_version
    libvirt_version=$(virsh --version 2>/dev/null | head -1 || true)
    log "libvirt: ${libvirt_version:-unknown (virsh not installed)}"

    local line
    while IFS= read -r line; do
        if [ -n "$line" ]; then
            log "  $line"
        fi
    done < <(iptables -S FORWARD 2>&1 || true)

    local chain
    for chain in LIBVIRT_FWO LIBVIRT_FWI; do
        while IFS= read -r line; do
            if [ -n "$line" ]; then
                log "  $line"
            fi
        done < <(iptables -S "$chain" 2>&1 || true)
    done
    log "--- end firewall state ---"
}

# ---------------------------------------------------------------------------
# uBridge — built from source at a pinned tag; no packaged version exists.
# ---------------------------------------------------------------------------
build_ubridge() {
    if [ "$(binary_version "$LOCAL_BIN/ubridge")" = "$UBRIDGE_VERSION" ]; then
        log "uBridge $UBRIDGE_VERSION already installed — skipping build"
    else
        log "building uBridge $UBRIDGE_TAG from source"
        run rm -rf "$SRC_DIR/ubridge"
        run git clone --depth 1 --branch "$UBRIDGE_TAG" https://github.com/GNS3/ubridge.git "$SRC_DIR/ubridge"
        run make -C "$SRC_DIR/ubridge"
        run install -m 755 "$SRC_DIR/ubridge/ubridge" "$LOCAL_BIN/ubridge"
    fi

    # Applied on the skip path too, not just after a build: `install`
    # replaces the file and drops its capabilities, and a binary that was
    # put there by hand may never have had them. assert_ubridge is fatal on
    # their absence, and setcap on a binary that already has them is a
    # no-op, so re-applying unconditionally is the cheap side.
    run setcap cap_net_admin,cap_net_raw=ep "$LOCAL_BIN/ubridge"
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
    if [ "$(binary_version "$LOCAL_BIN/vpcs")" = "$VPCS_VERSION" ]; then
        log "VPCS $VPCS_VERSION already installed — skipping build"
        return 0
    fi

    log "building VPCS $VPCS_TAG from source"
    run rm -rf "$SRC_DIR/vpcs"
    run git clone https://github.com/GNS3/vpcs.git "$SRC_DIR/vpcs"
    run git -C "$SRC_DIR/vpcs" checkout "$VPCS_TAG"

    if [ "$DRY_RUN" = 1 ]; then
        log "DRY-RUN would truncate $SRC_DIR/vpcs/src/getopt.h"
    else
        : > "$SRC_DIR/vpcs/src/getopt.h"
    fi
    run make -C "$SRC_DIR/vpcs/src" -f Makefile.linux CC="gcc -fcommon"
    run install -m 755 "$SRC_DIR/vpcs/src/vpcs" "$LOCAL_BIN/vpcs"
}

# ---------------------------------------------------------------------------
# gns3-server — pipx-managed but installed system-wide (PIPX_HOME /
# PIPX_BIN_DIR), not per-user, so it's on PATH regardless of which user
# account ends up running it.
# ---------------------------------------------------------------------------
install_gns3_server() {
    local installed
    installed=$(binary_version "$LOCAL_BIN/gns3server" --version)

    # `pipx install` exits non-zero when the package is already installed,
    # and under `set -e` that killed the entire script — which is what made
    # a second run of this script impossible, sentinel or no sentinel.
    # --force is used only on a real mismatch so that the common re-run
    # case doesn't rebuild a working venv for nothing.
    if [ "$installed" = "$GNS3_SERVER_VERSION" ]; then
        log "gns3-server $GNS3_SERVER_VERSION already installed — skipping"
        return 0
    fi

    if [ -n "$installed" ]; then
        log "installing gns3-server==$GNS3_SERVER_VERSION via pipx (replacing $installed)"
    else
        log "installing gns3-server==$GNS3_SERVER_VERSION via pipx"
    fi
    run env "PIPX_HOME=$PIPX_ROOT" "PIPX_BIN_DIR=$LOCAL_BIN" \
        pipx install --force "gns3-server==$GNS3_SERVER_VERSION"
}

# ---------------------------------------------------------------------------
# Assertions — one per version/capability gate. Exit non-zero on the first
# failure so a broken provision is visible in journalctl instead of
# surfacing days later as a node that won't start.
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
    # dynamips's own exit code.
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

assert_qemu_img() {
    command -v qemu-img >/dev/null 2>&1 \
        || fatal "qemu-img not found — gns3server needs it alongside qemu-system-x86_64 for every QEMU node"
    log "OK: qemu-img present"
}

assert_ubridge() {
    getcap "$LOCAL_BIN/ubridge" 2>/dev/null | grep -q "cap_net_admin" \
        || fatal "ubridge is missing cap_net_admin/cap_net_raw capabilities"
    log "OK: ubridge has required capabilities"
}

assert_ubridge_version() {
    # binary_version already swallows the probe's exit status (same reason
    # as assert_dynamips: only its output can be trusted), so this compares
    # the parsed version and never sees ubridge's own return code.
    local version
    version=$(binary_version "$LOCAL_BIN/ubridge")
    [ "$version" = "$UBRIDGE_VERSION" ] \
        || fatal "ubridge reports '${version:-nothing}', expected $UBRIDGE_VERSION"
    log "OK: ubridge is $UBRIDGE_VERSION"
}

assert_libvirt() {
    # A GNS3 NAT node is unusable unless `default` is running with its
    # bridge addressed, so this is fatal — but not immediately. libvirt is
    # socket-activated, so on a cold boot the network may not be up yet at
    # the moment the startup script reaches this point; retry briefly
    # before declaring failure.
    #
    # Deliberately NOT checked: virbr0's link state. A bridge takes its
    # carrier from its ports, so with no guest attached virbr0 reads
    # DOWN / NO-CARRIER on a perfectly healthy machine. Only the address
    # and the network's own state say anything.
    local attempt active autostart
    for attempt in $(seq 1 "$LIBVIRT_NET_RETRIES"); do
        active=$(libvirt_net_field Active:)
        autostart=$(libvirt_net_field Autostart:)
        if [ "$active" = "yes" ] && [ "$autostart" = "yes" ] \
           && ip -4 addr show virbr0 2>/dev/null | grep -Fq "inet $LIBVIRT_NET_ADDR"; then
            log "OK: libvirt network '$LIBVIRT_NET' active + autostart, virbr0 has $LIBVIRT_NET_ADDR"
            return 0
        fi
        if [ "$attempt" -lt "$LIBVIRT_NET_RETRIES" ]; then
            sleep "$LIBVIRT_NET_RETRY_SLEEP"
        fi
    done

    fatal "libvirt network '$LIBVIRT_NET' is not usable:" \
          "active=${active:-unknown} autostart=${autostart:-unknown};" \
          "virbr0: $(ip -4 addr show virbr0 2>&1 | tr '\n' ' ' || true)"
}

run_assertions() {
    assert_vpcs
    assert_dynamips
    assert_kvm
    assert_qemu_img
    assert_ubridge
    assert_ubridge_version
    assert_libvirt
}

main() {
    parse_args "$@"

    if [ -f "$SENTINEL" ]; then
        log "sentinel present ($SENTINEL) — skipping install, running checks only"
    else
        install_packages
        build_ubridge
        build_vpcs
        install_gns3_server
    fi

    # Outside the sentinel gate on purpose: the network has to be brought
    # up on every boot, and the firewall report is worth having in the
    # journal of every boot, not only the provisioning one.
    ensure_libvirt_network
    report_firewall_state

    if [ "$DRY_RUN" = 1 ]; then
        # Assertions check the result of steps a dry run did not perform,
        # so on a machine that isn't provisioned yet they would report
        # failures caused by the dry run itself. Skip them and say so.
        log "DRY-RUN: skipping assertions — they check the result of steps that did not run"
        log "DRY-RUN complete, nothing was changed"
        return 0
    fi

    run_assertions

    if [ -f "$SENTINEL" ]; then
        log "provision check complete"
    else
        mkdir -p "$SENTINEL_DIR"
        date -u +%FT%TZ > "$SENTINEL"
        log "provision complete, sentinel written"
    fi
}

# Sourcing this file gets the functions and none of the side effects, which
# is how the unit tests exercise the guard logic without a VM.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
