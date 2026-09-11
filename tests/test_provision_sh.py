"""Unit tests for provision.sh's guard logic.

provision.sh is a GCE startup script that expects to be root on a real
Ubuntu VM, so what's testable here is only the part that decides *whether*
to do something: the skip guards, the install order, the libvirt state
machine and the --dry-run wrapper. Everything is exercised by sourcing the
script (its last line is guarded on BASH_SOURCE, so sourcing gets the
functions and none of the side effects) with stub executables ahead of the
real ones on PATH. Each stub appends its own argv to a log file, so a test
can assert both what ran and what didn't.

Not bats, which is the obvious tool for this, because it isn't available in
the dev container; pytest already is, and is what the rest of the suite
uses.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""

import os
import subprocess
from pathlib import Path

import pytest

PROVISION = Path(__file__).resolve().parent.parent / "provision.sh"

# Stubbed as no-ops for every test; individual tests re-stub what they care
# about. rm/git/make/install/setcap are here so a test that lets a build
# through can't touch the real filesystem.
DEFAULT_STUBS = (
    "apt-get",
    "apt-cache",
    "apt-mark",
    "dpkg",
    "git",
    "install",
    "ip",
    "iptables",
    "make",
    "pipx",
    "rm",
    "setcap",
    "systemctl",
    "virsh",
)


class Harness:
    def __init__(self, tmp_path):
        self.bin = tmp_path / "stubbin"
        self.bin.mkdir()
        self.local_bin = tmp_path / "localbin"
        self.local_bin.mkdir()
        self.src = tmp_path / "src"
        self.src.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.write_text("")
        for name in DEFAULT_STUBS:
            self.stub(name)

    def stub(self, name, body="", exit_code=0):
        """Put an executable named `name` ahead of the real one on PATH."""
        path = self.bin / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            'printf "%s\\n" "${0##*/} $*" >> "$STUB_LOG"\n'
            f"{body}\n"
            f"exit {exit_code}\n"
        )
        path.chmod(0o755)

    def installed_binary(self, name, version_line, flag="-v"):
        """A binary in LOCAL_BIN that answers a version probe, as the real
        vpcs/ubridge/gns3server do."""
        path = self.local_bin / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            f'if [ "$1" = "{flag}" ]; then echo "{version_line}"; fi\n'
            "exit 0\n"
        )
        path.chmod(0o755)

    def stub_virsh(self, defined=True, active="yes", autostart="yes"):
        net_info = (
            "printf 'Name:            default\\n"
            f"Active:          {active}\\n"
            f"Autostart:       {autostart}\\n"
            "Bridge:          virbr0\\n'"
            if defined
            else "exit 1"
        )
        self.stub(
            "virsh",
            body=(
                'case "$*" in\n'
                f"  *net-info*) {net_info} ;;\n"
                '  *--version*) echo "10.0.0" ;;\n'
                "esac\n"
            ),
        )

    def stub_virbr0(self, address="192.168.122.1/24"):
        self.stub("ip", body=f'echo "    inet {address} brd 192.168.122.255 scope global virbr0"')

    def run(self, snippet, dry_run=False, env=None):
        environ = dict(os.environ)
        environ["PATH"] = f"{self.bin}:{environ['PATH']}"
        environ["STUB_LOG"] = str(self.log)
        # Keep assert_libvirt's retry loop from turning a failing test into
        # a 20-second one.
        environ["LIBVIRT_NET_RETRIES"] = "2"
        environ["LIBVIRT_NET_RETRY_SLEEP"] = "0"
        environ.update(env or {})
        preamble = (
            f'source "{PROVISION}"; '
            f'LOCAL_BIN="{self.local_bin}"; SRC_DIR="{self.src}"; '
            f"DRY_RUN={1 if dry_run else 0}; "
        )
        return subprocess.run(
            ["bash", "-c", preamble + snippet],
            env=environ,
            capture_output=True,
            text=True,
        )

    @property
    def calls(self):
        return self.log.read_text().splitlines()

    def called(self, needle):
        return [c for c in self.calls if needle in c]


@pytest.fixture
def sh(tmp_path):
    return Harness(tmp_path)


# ---------------------------------------------------------------------------
# sourcing / argument parsing
# ---------------------------------------------------------------------------


def test_sourcing_does_not_run_main(sh):
    # The BASH_SOURCE guard on the last line is what makes every other test
    # here possible; if it regresses, sourcing would try to provision the
    # dev container.
    result = sh.run('echo "sourced ok"')
    assert result.returncode == 0
    assert "sourced ok" in result.stdout
    assert sh.calls == []


def test_parse_args_sets_dry_run(sh):
    result = sh.run('parse_args --dry-run; echo "DRY_RUN=$DRY_RUN"')
    assert result.returncode == 0
    assert "DRY_RUN=1" in result.stdout


def test_parse_args_rejects_unknown_argument(sh):
    result = sh.run("parse_args --wat")
    assert result.returncode == 1
    assert "unknown argument: --wat" in result.stderr


def test_run_executes_normally_but_only_prints_under_dry_run(sh):
    assert sh.run("run git clone whatever").returncode == 0
    assert sh.called("git clone whatever")

    sh.log.write_text("")
    result = sh.run("run git clone whatever", dry_run=True)
    assert result.returncode == 0
    assert "DRY-RUN would run: git clone whatever" in result.stdout
    assert sh.calls == []


# ---------------------------------------------------------------------------
# apt
# ---------------------------------------------------------------------------


def test_apt_install_one_skips_installed_package(sh):
    sh.stub("dpkg")  # exit 0 == already installed
    result = sh.run("apt_install_one libvirt-daemon-system")
    assert result.returncode == 0
    assert "libvirt-daemon-system already installed" in result.stdout
    assert sh.called("apt-get install") == []


def test_apt_install_one_installs_missing_package(sh):
    # Missing at first check, present at the post-install check.
    sh.stub(
        "dpkg",
        body='[ -f "$STUB_LOG.installed" ] || exit 1',
    )
    sh.stub("apt-get", body='touch "$STUB_LOG.installed"')
    result = sh.run("apt_install_one dnsmasq-base")
    assert result.returncode == 0
    assert sh.called("apt-get install -y --no-install-recommends dnsmasq-base")


def test_apt_install_one_fatal_without_a_candidate(sh):
    sh.stub("dpkg", exit_code=1)
    sh.stub("apt-cache", exit_code=1)
    result = sh.run("apt_install_one nonesuch")
    assert result.returncode == 1
    assert "apt has no candidate for 'nonesuch'" in result.stderr
    assert sh.called("apt-get install") == []


def test_apt_install_one_simulates_under_dry_run(sh):
    sh.stub("dpkg", exit_code=1)
    result = sh.run("apt_install_one libvirt-clients", dry_run=True)
    assert result.returncode == 0
    # -s is the real resolver, so the dry run reports what apt would
    # actually do rather than guessing.
    assert sh.called("apt-get install -s -y --no-install-recommends libvirt-clients")


def test_apt_install_one_fatal_when_simulation_fails(sh):
    sh.stub("dpkg", exit_code=1)
    sh.stub("apt-get", exit_code=100)
    result = sh.run("apt_install_one libvirt-clients", dry_run=True)
    assert result.returncode == 1
    assert "cannot be installed" in result.stderr


def test_install_packages_orders_dnsmasq_before_libvirt(sh):
    sh.stub("dpkg", exit_code=1)
    sh.stub("apt-get")  # claims success; dpkg still says "not installed"
    result = sh.run("install_packages", dry_run=True)
    assert result.returncode == 0

    order = [
        line.split()[-1]
        for line in sh.calls
        if line.startswith("apt-get install")
    ]
    for pkg in ("dnsmasq-base", "libvirt-daemon-system", "libvirt-clients", "iptables"):
        assert pkg in order, f"{pkg} was never installed"
    # dnsmasq-base is only a Recommends of libvirt-daemon-system and this
    # script installs with --no-install-recommends, so libvirt's postinst
    # can only start the default network if dnsmasq is already there.
    assert order.index("dnsmasq-base") < order.index("libvirt-daemon-system")


def test_install_packages_installs_qemu_utils(sh):
    # qemu-utils (qemu-img) is only a Recommends of qemu-system-x86, and
    # this script installs with --no-install-recommends, so it has to be
    # named explicitly or gns3server fails every QEMU node with "Could not
    # find qemu-img" — see assert_qemu_img.
    sh.stub("dpkg", exit_code=1)
    sh.stub("apt-get")
    result = sh.run("install_packages", dry_run=True)
    assert result.returncode == 0
    assert sh.called("apt-get install -s -y --no-install-recommends qemu-utils")


def test_install_packages_marks_iptables_manual(sh):
    # The one thing an install alone can't achieve: iptables arrives as
    # docker.io's automatic dependency, so apt_install_one returns early and
    # the auto flag survives. Without the mark, removing Docker later lets
    # autoremove take iptables and libvirt networks silently stop starting.
    sh.stub("dpkg")  # everything already installed
    result = sh.run("install_packages", dry_run=True)
    assert result.returncode == 0
    assert "DRY-RUN would run: apt-mark manual iptables" in result.stdout


# ---------------------------------------------------------------------------
# version guards — the reason a second run of this script is possible at all
# ---------------------------------------------------------------------------


def test_binary_version_reports_nothing_for_absent_binary(sh):
    result = sh.run('echo "[$(binary_version "$LOCAL_BIN/nope")]"')
    assert result.returncode == 0
    assert "[]" in result.stdout


def test_binary_version_parses_a_version_banner(sh):
    sh.installed_binary("ubridge", "uBridge version 1.2.1")
    result = sh.run('echo "[$(binary_version "$LOCAL_BIN/ubridge")]"')
    assert "[1.2.1]" in result.stdout


def test_binary_version_survives_a_nonzero_probe(sh):
    path = sh.local_bin / "ubridge"
    path.write_text('#!/usr/bin/env bash\necho "uBridge version 1.2.1"\nexit 1\n')
    path.chmod(0o755)
    result = sh.run('echo "[$(binary_version "$LOCAL_BIN/ubridge")]"')
    assert result.returncode == 0
    assert "[1.2.1]" in result.stdout


def test_build_ubridge_skips_when_the_pinned_version_is_installed(sh):
    sh.installed_binary("ubridge", "uBridge version 1.2.1")
    result = sh.run("build_ubridge")
    assert result.returncode == 0
    assert sh.called("git clone") == []
    # setcap runs anyway: `install` drops capabilities, and a hand-placed
    # binary may never have had them, while assert_ubridge is fatal without.
    assert sh.called("setcap cap_net_admin")


def test_build_ubridge_rebuilds_a_different_version(sh):
    sh.installed_binary("ubridge", "uBridge version 1.2.2")
    result = sh.run("build_ubridge")
    assert result.returncode == 0
    clone = sh.called("git clone")
    assert clone, "expected a rebuild"
    # Pinned, not master: an unpinned clone installed whatever HEAD was on
    # the day it ran.
    assert "--branch v1.2.1" in clone[0]


def test_build_vpcs_skips_when_the_pinned_version_is_installed(sh):
    sh.installed_binary("vpcs", "Welcome to Virtual PC Simulator, version 0.6.2")
    result = sh.run("build_vpcs")
    assert result.returncode == 0
    assert sh.called("git clone") == []


def test_build_vpcs_builds_when_absent(sh):
    # The real clone creates this; git is stubbed here, and the getopt.h
    # truncation is a plain redirect rather than a `run` command, so the
    # directory has to exist for the build path to get past it.
    (sh.src / "vpcs" / "src").mkdir(parents=True)
    result = sh.run("build_vpcs")
    assert result.returncode == 0
    assert sh.called("git clone")
    assert sh.called("make -C")


def test_install_gns3_server_skips_when_the_pinned_version_is_installed(sh):
    sh.installed_binary("gns3server", "2.2.61", flag="--version")
    result = sh.run("install_gns3_server")
    assert result.returncode == 0
    assert sh.called("pipx install") == []


def test_install_gns3_server_forces_over_a_mismatch(sh):
    # `pipx install` exits non-zero when the package is already there, which
    # under set -e killed the whole script — the one breakage that made any
    # re-run impossible.
    sh.installed_binary("gns3server", "2.2.60", flag="--version")
    result = sh.run("install_gns3_server")
    assert result.returncode == 0
    assert sh.called("pipx install --force gns3-server==2.2.61")


def test_install_gns3_server_installs_when_absent(sh):
    result = sh.run("install_gns3_server")
    assert result.returncode == 0
    assert sh.called("pipx install --force gns3-server==2.2.61")


# ---------------------------------------------------------------------------
# libvirt
# ---------------------------------------------------------------------------


def test_libvirt_network_unit_prefers_virtnetworkd(sh):
    sh.stub("systemctl", body='case "$*" in *virtnetworkd*) echo "virtnetworkd.service enabled";; esac')
    result = sh.run("libvirt_network_unit")
    assert result.stdout.strip() == "virtnetworkd.service"


def test_libvirt_network_unit_falls_back_to_libvirtd(sh):
    # Ubuntu 24.04's libvirt 10.0.0 is still monolithic — no virtnetworkd.
    sh.stub("systemctl", body='case "$*" in *libvirtd*) echo "libvirtd.service enabled";; esac')
    result = sh.run("libvirt_network_unit")
    assert result.stdout.strip() == "libvirtd.service"


def test_ensure_libvirt_network_is_a_no_op_when_already_up(sh):
    sh.stub("systemctl", body='case "$*" in *libvirtd*) echo "libvirtd.service enabled";; esac')
    sh.stub_virsh(defined=True, active="yes", autostart="yes")
    result = sh.run("ensure_libvirt_network")
    assert result.returncode == 0
    assert sh.called("net-define") == []
    assert sh.called("net-autostart") == []
    assert sh.called("net-start") == []
    assert sh.called("systemctl enable --now libvirtd.service")


def test_ensure_libvirt_network_starts_an_inactive_network(sh):
    sh.stub("systemctl", body='case "$*" in *libvirtd*) echo "libvirtd.service enabled";; esac')
    sh.stub_virsh(defined=True, active="no", autostart="no")
    result = sh.run("ensure_libvirt_network")
    assert result.returncode == 0
    assert sh.called("net-autostart default")
    assert sh.called("net-start default")
    assert sh.called("net-define") == []


def test_ensure_libvirt_network_defines_from_xml_only_as_a_fallback(sh, tmp_path):
    # libvirt-daemon-config-network normally ships and autostarts `default`,
    # so this path is for a machine where it was removed or never landed.
    xml = tmp_path / "default.xml"
    xml.write_text("<network/>")
    sh.stub("systemctl", body='case "$*" in *libvirtd*) echo "libvirtd.service enabled";; esac')
    sh.stub_virsh(defined=False)
    result = sh.run(f'LIBVIRT_NET_XML="{xml}"; ensure_libvirt_network')
    assert result.returncode == 0
    assert sh.called(f"net-define {xml}")


def test_ensure_libvirt_network_fatal_when_libvirt_is_absent(sh, tmp_path):
    # A VM provisioned before libvirt was added here keeps its sentinel and
    # skips install_packages, so it lands exactly here.
    (sh.bin / "virsh").unlink()
    result = sh.run("ensure_libvirt_network")
    assert result.returncode == 1
    assert "libvirt is not installed" in result.stderr
    assert "upgrade" in result.stderr


def test_ensure_libvirt_network_tolerates_absent_libvirt_under_dry_run(sh):
    (sh.bin / "virsh").unlink()
    result = sh.run("ensure_libvirt_network", dry_run=True)
    assert result.returncode == 0
    assert "DRY-RUN" in result.stdout


def test_ensure_libvirt_network_changes_nothing_under_dry_run(sh):
    sh.stub("systemctl", body='case "$*" in *libvirtd*) echo "libvirtd.service enabled";; esac')
    sh.stub_virsh(defined=True, active="no", autostart="no")
    result = sh.run("ensure_libvirt_network", dry_run=True)
    assert result.returncode == 0
    assert "DRY-RUN would run: virsh -c qemu:///system net-start default" in result.stdout
    assert sh.called("net-start") == []


def test_assert_libvirt_passes_when_active_autostart_and_addressed(sh):
    sh.stub_virsh(defined=True, active="yes", autostart="yes")
    sh.stub_virbr0()
    result = sh.run("assert_libvirt")
    assert result.returncode == 0
    assert "OK: libvirt network" in result.stdout


def test_assert_libvirt_fatal_when_the_network_is_inactive(sh):
    sh.stub_virsh(defined=True, active="no", autostart="yes")
    sh.stub_virbr0()
    result = sh.run("assert_libvirt")
    assert result.returncode == 1
    assert "is not usable" in result.stderr


def test_assert_libvirt_fatal_when_virbr0_has_no_address(sh):
    sh.stub_virsh(defined=True, active="yes", autostart="yes")
    sh.stub("ip", exit_code=1)
    result = sh.run("assert_libvirt")
    assert result.returncode == 1
    assert "is not usable" in result.stderr


def test_assert_libvirt_retries_before_giving_up(sh):
    # libvirt is socket-activated, so on a cold boot the network can still
    # be coming up when the startup script reaches this point.
    sh.stub(
        "virsh",
        body=(
            'case "$*" in *net-info*)\n'
            '  n=$(cat "$STUB_LOG.attempts" 2>/dev/null || echo 0); n=$((n+1));\n'
            '  echo "$n" > "$STUB_LOG.attempts";\n'
            '  if [ "$n" -ge 3 ]; then active=yes; else active=no; fi;\n'
            "  printf 'Name:  default\\nActive:  %s\\nAutostart:  yes\\n' \"$active\";;\n"
            "esac\n"
        ),
    )
    sh.stub_virbr0()
    result = sh.run("assert_libvirt", env={"LIBVIRT_NET_RETRIES": "5"})
    assert result.returncode == 0
    assert "OK: libvirt network" in result.stdout


def test_assert_libvirt_ignores_virbr0_link_state(sh):
    # A bridge borrows carrier from its ports, so virbr0 reads DOWN /
    # NO-CARRIER on a healthy machine with no guest attached. Only the
    # address matters.
    sh.stub_virsh(defined=True, active="yes", autostart="yes")
    sh.stub(
        "ip",
        body=(
            "echo '3: virbr0: <NO-CARRIER,BROADCAST,MULTICAST,UP> state DOWN'\n"
            "echo '    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0'"
        ),
    )
    result = sh.run("assert_libvirt")
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# firewall report — informational, and must never be able to fail a boot
# ---------------------------------------------------------------------------


def test_report_firewall_state_logs_the_chain_order(sh):
    sh.stub(
        "iptables",
        body=(
            'case "$1$2" in\n'
            '  *FORWARD) echo "-P FORWARD DROP"; echo "-A FORWARD -j LIBVIRT_FWX";;\n'
            '  *LIBVIRT_FWO) echo "-A LIBVIRT_FWO -s 192.168.122.0/24 -i virbr0 -j ACCEPT";;\n'
            "esac\n"
        ),
    )
    result = sh.run("report_firewall_state")
    assert result.returncode == 0
    assert "-P FORWARD DROP" in result.stdout
    assert "-A FORWARD -j LIBVIRT_FWX" in result.stdout
    assert "-s 192.168.122.0/24 -i virbr0 -j ACCEPT" in result.stdout


def test_report_firewall_state_never_fails_the_boot(sh):
    (sh.bin / "iptables").unlink()
    (sh.bin / "virsh").unlink()
    result = sh.run("report_firewall_state; echo survived")
    assert result.returncode == 0
    assert "survived" in result.stdout
    assert "libvirt: unknown" in result.stdout


def test_report_firewall_state_survives_a_failing_iptables(sh):
    sh.stub("iptables", body='echo "iptables: Permission denied" >&2', exit_code=1)
    result = sh.run("report_firewall_state; echo survived")
    assert result.returncode == 0
    assert "survived" in result.stdout


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------


def test_assert_qemu_img_passes_when_present(sh):
    sh.stub("qemu-img")
    result = sh.run("assert_qemu_img")
    assert result.returncode == 0
    assert "OK: qemu-img present" in result.stdout


def test_assert_qemu_img_fatal_when_missing(sh):
    result = sh.run("assert_qemu_img")
    assert result.returncode == 1
    assert "qemu-img not found" in result.stderr


def test_assert_ubridge_version_accepts_the_pin(sh):
    sh.installed_binary("ubridge", "uBridge version 1.2.1")
    result = sh.run("assert_ubridge_version")
    assert result.returncode == 0
    assert "OK: ubridge is 1.2.1" in result.stdout


def test_assert_ubridge_version_rejects_another_version(sh):
    sh.installed_binary("ubridge", "uBridge version 1.2.2")
    result = sh.run("assert_ubridge_version")
    assert result.returncode == 1
    assert "expected 1.2.1" in result.stderr


def test_assert_ubridge_version_rejects_a_missing_binary(sh):
    result = sh.run("assert_ubridge_version")
    assert result.returncode == 1
    assert "reports 'nothing'" in result.stderr
