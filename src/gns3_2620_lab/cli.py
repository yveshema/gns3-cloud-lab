"""create, enroll, start/refresh, stop, status.

The VM's external IP and the user's public IP both change every session, in
opposite directions. start/refresh is the only command that refreshes both
the firewall source range and the GUI config; the rest are comparatively
simple.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import io
import secrets
import shutil
import sys

from . import gcp, gns3conf, setup_help

DEFAULT_INSTANCE = "gns3-lab"
# N1/N2/C2 only — nested virtualization needs a Haswell+ host CPU, which
# E2/N2D/AMD/Arm/memory-optimized machine types don't have.
DEFAULT_MACHINE_TYPE = "n2-standard-4"
DEFAULT_TAG = "gns3"
SERVER_PORT = 3080
GUI_USER = "admin"
CONSOLE_PORT_START = 5000
CONSOLE_PORT_END = 5050
# Single source of truth for the console range: FIREWALL_PORTS is derived
# from CONSOLE_PORT_START/END rather than a separately hardcoded string, so
# the two can't drift out of sync with each other.
FIREWALL_PORTS = [f"tcp:{SERVER_PORT}", f"tcp:{CONSOLE_PORT_START}-{CONSOLE_PORT_END}"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gns3-2620-lab",
        description="Per-user GCP fallback for BCIT networking-2620 GNS3 labs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Don't remember your VM's name? Run with no command (or `scan` explicitly)\n"
            "to list every instance in the project.\n"
            "\n"
            "Typical first use:      create, then start\n"
            "Every following session: start (or refresh, same thing)\n"
            "Done for the day:        stop\n"
            "Not sure what's going on: status\n"
            "\n"
            "GNS3 must be closed before start/refresh/stop — they change the config\n"
            "it reads from, and a running copy won't see the change and may write\n"
            "its own state back over it when it later closes."
        ),
    )
    parser.add_argument(
        "--project", default=None, help="GCP project (default: active gcloud configuration's project)"
    )
    parser.add_argument(
        "--instance", default=DEFAULT_INSTANCE, help=f"VM instance name (default: {DEFAULT_INSTANCE})"
    )
    parser.add_argument(
        "--zone", default=None, help="GCP zone (default: active gcloud configuration's compute/zone)"
    )
    parser.add_argument(
        "--gcloud-config", default=None, help="gcloud configuration name to use for this run"
    )

    # Not required: no subcommand at all defaults to `scan` (see main()) —
    # forgetting a VM's name is the most likely first mistake, so the bare
    # invocation should help rather than just print a usage error.
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", help="List every instance in the project (default when run with no command)")
    create_parser = sub.add_parser("create", help="Create the firewall rule and a brand-new VM")
    create_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be created without creating it"
    )
    sub.add_parser(
        "enroll",
        help="Adopt a VM that already exists in GCP but wasn't created by this tool",
    )
    sub.add_parser(
        "start",
        aliases=["refresh"],
        help=(
            "Start the VM and refresh firewall/GUI config for your current network. "
            "Run this the first time to boot the lab, and again any time your "
            "location changes — safe to run repeatedly. Requires GNS3 to be closed."
        ),
    )
    sub.add_parser("stop", help="Stop the VM and restore your GUI config. Requires GNS3 to be closed.")
    sub.add_parser("status", help="Print VM and server status")
    return parser


def _firewall_name(instance: str) -> str:
    return f"{instance}-gns3"


def _build_context(args, gcloud_exe: str, *, resolve_zone: bool = True) -> gcp.GcpContext:
    project = args.project or gcp.get_active_project(gcloud_exe, args.gcloud_config)
    # scan is project-wide and never reads ctx.zone — it shouldn't demand a
    # configured zone just to list what's out there, which defeats its
    # whole point as a "don't know my zone yet" discovery command.
    if resolve_zone:
        zone = args.zone or gcp.get_active_zone(gcloud_exe, args.gcloud_config)
    else:
        zone = args.zone
    return gcp.GcpContext(
        gcloud_exe=gcloud_exe,
        project=project,
        zone=zone,
        instance=args.instance,
        config_name=args.gcloud_config,
    )


@contextlib.contextmanager
def _progress(out, label: str):
    """Print label, a dot per on_tick() call, then a newline once done —
    the only feedback available for our own poll loops, which (unlike
    create/start/stop_instance) have no gcloud output of their own to show
    live. Without it, a silent terminal for over a minute is
    indistinguishable from a hang."""
    print(f"{label}...", end="", file=out, flush=True)
    try:
        yield lambda: print(".", end="", file=out, flush=True)
    finally:
        print(file=out)


def _load_state() -> dict:
    return gns3conf.load_json(gns3conf.wrapper_state_path())


def _save_state(state: dict) -> None:
    gns3conf.save_json(gns3conf.wrapper_state_path(), state)


def _state_key(ctx: gcp.GcpContext) -> str:
    return f"{ctx.project}/{ctx.zone}/{ctx.instance}"


def _ensure_firewall_rule(ctx: gcp.GcpContext, *, name: str, source_range: str) -> None:
    if gcp.firewall_rule_exists(ctx, name):
        gcp.update_firewall_source_range(ctx, name=name, source_range=source_range)
    else:
        gcp.create_firewall_rule(
            ctx, name=name, tags=[DEFAULT_TAG], source_range=source_range, ports=FIREWALL_PORTS
        )


def _boot_and_wait_for_ssh(ctx: gcp.GcpContext, status: str, out) -> tuple:
    """Start the VM if it isn't already RUNNING, then wait for an external
    IP and for SSH to accept connections. Shared by start and enroll: both
    need the VM reachable over SSH before doing anything with it — enroll
    especially, since it must confirm SSH access before trusting whatever
    _discover_or_generate_credentials finds (or doesn't) rather than ever
    guessing at credentials on a VM it didn't create.

    Returns (external_ip, ssh_ready). external_ip is None if it never got
    one, in which case ssh_ready is always False. wait_for_status (below)
    raises GcpError on its own timeout, so by the time this returns, the
    instance really is RUNNING.
    """
    if status != "RUNNING":
        print(f"Starting {ctx.instance}...", file=out)  # gcloud shows its own progress here
        gcp.start_instance(ctx)
        with _progress(out, "Waiting for instance to report RUNNING") as tick:
            gcp.wait_for_status(ctx, "RUNNING", on_tick=tick)

    with _progress(out, "Waiting for an external IP") as tick:
        external_ip = gcp.wait_for_external_ip(ctx, on_tick=tick)
    if external_ip is None:
        return None, False

    # RUNNING (above) only means GCE started booting the VM, not that sshd
    # is accepting connections yet — the SSH call just below can hit
    # "Connection refused" moments after this point and succeed on retry.
    with _progress(out, "Waiting for SSH") as tick:
        ssh_ready = gcp.wait_for_ssh_ready(ctx, on_tick=tick)
    return external_ip, ssh_ready


def _refuse_if_gui_running(out) -> bool:
    if not gns3conf.gui_is_running():
        return False
    print(
        "GNS3 is currently running — close it first, then try again. "
        "This command changes the GUI config file GNS3 reads from, and a "
        "running copy won't pick that up (and may overwrite it when it closes).",
        file=out,
    )
    return True


# Both client-side files start/refresh patches (gns3conf module docstring)
# get backed up and restored together, as one unit — either both are
# touched by a session or neither is, so one "is a backup held" flag can
# cover both without them getting out of sync with each other. Attribute
# names, not direct function references: this dict is built once at import
# time, and a direct reference to e.g. gns3conf.gns3_gui_config_path taken
# then would not see a test's later monkeypatch.setattr(gns3conf, ...) —
# looking it up via getattr(gns3conf, name) at call time does.
_BACKED_UP_FILES = {
    "gui_conf": ("gns3_gui_config_path", "wrapper_gui_conf_backup_path"),
    "local_server_conf": ("gns3_local_server_conf_path", "wrapper_local_server_conf_backup_path"),
}


def _take_gui_conf_backup_if_needed(state: dict) -> None:
    # Only the first start/refresh in a session takes a backup: state
    # already holding "_gui_backup" means one is outstanding, and a second
    # backup here would capture our own already-patched config as if it
    # were the user's original.
    if "_gui_backup" in state:
        return
    held = {}
    for name, (conf_path_attr, backup_path_attr) in _BACKED_UP_FILES.items():
        conf_path = getattr(gns3conf, conf_path_attr)()
        existed = conf_path.exists()
        if existed:
            backup_path = getattr(gns3conf, backup_path_attr)()
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(conf_path, backup_path)
        held[name] = {"existed": existed}
    state["_gui_backup"] = held


def _restore_gui_conf_if_held(state: dict, out) -> None:
    held = state.pop("_gui_backup", None)
    if held is None:
        return
    for name, (conf_path_attr, backup_path_attr) in _BACKED_UP_FILES.items():
        conf_path = getattr(gns3conf, conf_path_attr)()
        if held[name]["existed"]:
            backup_path = getattr(gns3conf, backup_path_attr)()
            shutil.copy2(backup_path, conf_path)
            backup_path.unlink(missing_ok=True)
        else:
            conf_path.unlink(missing_ok=True)
    print("Restored your GNS3 GUI config to how it was before start/refresh.", file=out)


def _discover_or_generate_credentials(ctx: gcp.GcpContext) -> tuple:
    """Reuse credentials already on the instance (e.g. an instructor's
    hand-built VM someone may already have saved in a browser) rather than
    rotating them out from under a working setup; fall back to a freshly
    generated password if nothing's configured yet, same as create.

    Callers must confirm SSH access first (see _boot_and_wait_for_ssh) —
    the fallback below is only safe to treat as "nothing configured yet"
    once SSH is known to work, otherwise an unreachable VM with real
    credentials already on it would be indistinguishable from a fresh one.
    """
    # gns3_server.conf is INI (configparser), not JSON — same format
    # _remote_setup_command writes; see its comment for why.
    result = gcp.ssh_run(ctx, "cat ~/.config/GNS3/2.2/gns3_server.conf", check=False)
    if result.returncode == 0:
        config = configparser.RawConfigParser()
        try:
            config.read_string(result.stdout)
            return config.get("Server", "user"), config.get("Server", "password")
        except configparser.Error:
            pass
    return GUI_USER, secrets.token_urlsafe(18)


def _remote_setup_command(*, user: str, password: str) -> str:
    # host=0.0.0.0 and no console_host: the client connects directly to the
    # VM's external IP with no SSH tunnel, so the server and console
    # listeners must be reachable from outside — access is instead scoped
    # by the per-session firewall rule (_ensure_firewall_rule), not by
    # binding to localhost.
    #
    # gns3_server.conf is INI, not JSON: gns3-server's own
    # gns3server/config.py parses it with configparser. JSON here parses
    # fine on the client side (gns3_gui.conf really is JSON) but fails
    # silently on the server — configparser.Error on read is caught and
    # logged, not raised, so the server starts anyway with every one of
    # these settings unset and falls back to gns3-server's own built-in
    # defaults instead (auth off; console range 5000-10000, wider than the
    # firewall's 5000-5050).
    config = configparser.RawConfigParser()
    config.add_section("Server")
    config.set("Server", "host", "0.0.0.0")
    config.set("Server", "port", str(SERVER_PORT))
    config.set("Server", "auth", "True")
    config.set("Server", "user", user)
    config.set("Server", "password", password)
    config.set("Server", "console_start_port_range", str(CONSOLE_PORT_START))
    config.set("Server", "console_end_port_range", str(CONSOLE_PORT_END))
    buf = io.StringIO()
    config.write(buf)
    conf_ini = buf.getvalue()
    # A single-quoted heredoc delimiter disables shell expansion inside the
    # body, so the generated password is safe here even if it contains
    # characters like $ or ` (secrets.token_urlsafe never emits a literal
    # single quote, so the delimiter itself can't be broken out of).
    return (
        "set -e\n"
        # kvm and docker are both created by provision.sh, but an enrolled
        # VM (adopted, not created through this tool) may be missing either
        # group. Provisioning a VM this tool didn't create is out of scope
        # for enroll, which only adopts state — report exactly what's
        # missing and stop, rather than silently skipping it or installing
        # packages on someone else's VM unprompted.
        'missing=""\n'
        "for grp in kvm docker; do\n"
        '  getent group "$grp" >/dev/null 2>&1 || missing="$missing $grp"\n'
        "done\n"
        'if [ -n "$missing" ]; then\n'
        '  echo "Missing required group(s):$missing on this VM -- it was not fully '
        "provisioned by this tool's provision.sh. For docker: sudo apt-get install -y "
        'docker.io. Install the missing package(s) on the VM, then re-run start." >&2\n'
        "  exit 1\n"
        "fi\n"
        'id -nG "$(whoami)" | grep -qw kvm || sudo usermod -aG kvm "$(whoami)"\n'
        'id -nG "$(whoami)" | grep -qw docker || sudo usermod -aG docker "$(whoami)"\n'
        "mkdir -p ~/.config/GNS3/2.2; "
        "cat > ~/.config/GNS3/2.2/gns3_server.conf <<'GNS3_SERVER_CONF_EOF'\n"
        f"{conf_ini}"
        "GNS3_SERVER_CONF_EOF"
    )


def _remote_launch_command() -> str:
    # Separate --command call from _remote_setup_command: group membership
    # from usermod only applies to a new login, and each --command
    # invocation is its own fresh login shell.
    #
    # A PID file, not `pgrep -f gns3server`: the ssh --command string is
    # itself the invoking shell's full command line, and it necessarily
    # contains the literal text "gns3server" (the search pattern). `pgrep
    # -f` matches against full command lines and only excludes pgrep's own
    # PID, not its parent shell — so it always self-matches and the launch
    # never runs. `kill -0` checks the recorded PID is still alive without
    # matching on command-line text at all.
    return (
        'if [ -f ~/gns3server.pid ] && kill -0 "$(cat ~/gns3server.pid)" 2>/dev/null; then '
        "exit 0; "
        "fi\n"
        # `nohup gns3server ...` backgrounds and returns immediately —
        # it reports success even if gns3server then fails to launch
        # (e.g. "command not found"), and that failure is otherwise
        # invisible until wait_for_http_ready times out 120s later.
        # `gcloud compute ssh --command` runs a non-login, non-interactive
        # shell: ~/.bashrc/.profile PATH additions don't apply, so a pipx
        # install (which relies on those) can be genuinely on the machine
        # but not found here. Resolve an actual path first — PATH, then
        # /usr/local/bin, then ~/.local/bin — and fail fast with a clear
        # reason if none of them have it, instead of silently launching
        # nothing.
        "gns3server_bin=\"\"\n"
        'for candidate in "$(command -v gns3server 2>/dev/null)" /usr/local/bin/gns3server "$HOME/.local/bin/gns3server"; do\n'
        '  [ -n "$candidate" ] && [ -x "$candidate" ] && { gns3server_bin="$candidate"; break; }\n'
        "done\n"
        'if [ -z "$gns3server_bin" ]; then\n'
        '  echo "gns3server not found on PATH, /usr/local/bin, or ~/.local/bin -- is gns3-server installed on this VM?" >&2\n'
        "  exit 1\n"
        "fi\n"
        'setsid nohup "$gns3server_bin" </dev/null >~/gns3server.log 2>&1 & '
        "echo $! > ~/gns3server.pid"
    )


def cmd_scan(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    instances = gcp.list_instances(ctx.gcloud_exe, ctx.project, config=ctx.config_name)
    if not instances:
        print(f"No instances found in {ctx.project}.", file=out)
        return 0

    state = _load_state()
    tracked_names = {
        entry["instance"] for entry in state.values() if isinstance(entry, dict) and "instance" in entry
    }

    print(f"Instances in {ctx.project}:", file=out)
    for info in instances:
        name = info["name"]
        zone = gcp.instance_zone_name(info)
        known = "tracked by this tool" if name in tracked_names else "not tracked — run enroll to adopt it"
        print(f"  {name}  zone={zone}  status={info['status']}  ({known})", file=out)
    return 0


def cmd_create(ctx: gcp.GcpContext, out=sys.stdout, dry_run: bool = False) -> int:
    if gcp.instance_describe(ctx) is not None:
        print(
            f"Instance {ctx.instance} already exists in {ctx.project}/{ctx.zone}. Nothing to create.",
            file=out,
        )
        return 0

    public_ip = gcp.get_public_ipv4()
    fw_name = _firewall_name(ctx.instance)

    if dry_run:
        fw_exists = gcp.firewall_rule_exists(ctx, fw_name)
        print(f"Would create instance {ctx.instance} in {ctx.project}/{ctx.zone} ({DEFAULT_MACHINE_TYPE}).", file=out)
        print(
            f"Would {'update' if fw_exists else 'create'} firewall rule {fw_name} "
            f"(source range {public_ip}/32, ports {', '.join(FIREWALL_PORTS)}).",
            file=out,
        )
        print("Would save local state with a freshly generated password.", file=out)
        return 0

    password = secrets.token_urlsafe(18)
    _ensure_firewall_rule(ctx, name=fw_name, source_range=f"{public_ip}/32")

    gcp.create_instance(ctx, machine_type=DEFAULT_MACHINE_TYPE, tags=[DEFAULT_TAG])

    state = _load_state()
    state[_state_key(ctx)] = {
        "project": ctx.project,
        "zone": ctx.zone,
        "instance": ctx.instance,
        "user": GUI_USER,
        "password": password,
        "firewall_rule": fw_name,
    }
    _save_state(state)

    print(
        f"Created {ctx.instance} in {ctx.project}/{ctx.zone}. "
        "Give it a minute to finish booting, then run --start.",
        file=out,
    )
    return 0


def cmd_enroll(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    state = _load_state()
    key = _state_key(ctx)
    if key in state:
        print(f"{ctx.instance} is already enrolled ({ctx.project}/{ctx.zone}). Nothing to do.", file=out)
        return 0

    info = gcp.instance_describe(ctx)
    if info is None:
        print(
            f"Instance {ctx.instance} does not exist in {ctx.project}/{ctx.zone}. Nothing to enroll.",
            file=out,
        )
        return 1

    # Credential discovery below needs SSH — start the VM first if it isn't
    # already running, rather than falling straight to
    # _discover_or_generate_credentials without ever confirming SSH works.
    # Enrolling an already-configured server (e.g. an instructor's) must
    # never invent a password because SSH happened to be unreachable, and
    # have the next start silently overwrite real credentials it never
    # actually confirmed.
    external_ip, ssh_ready = _boot_and_wait_for_ssh(ctx, info["status"], out)
    if external_ip is None:
        print(f"{ctx.instance} never got an external IP. Not enrolling.", file=out)
        return 1
    if not ssh_ready:
        print(
            f"{ctx.instance} never accepted an SSH connection. Not enrolling — can't "
            "confirm whether real credentials already exist on it without SSH access.",
            file=out,
        )
        return 1

    fw_name = _firewall_name(ctx.instance)
    public_ip = gcp.get_public_ipv4()
    _ensure_firewall_rule(ctx, name=fw_name, source_range=f"{public_ip}/32")

    user, password = _discover_or_generate_credentials(ctx)

    state[key] = {
        "project": ctx.project,
        "zone": ctx.zone,
        "instance": ctx.instance,
        "user": user,
        "password": password,
        "firewall_rule": fw_name,
    }
    _save_state(state)

    print(f"Enrolled {ctx.instance} ({ctx.project}/{ctx.zone}). Run --start next.", file=out)
    return 0


def cmd_start(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    if _refuse_if_gui_running(out):
        return 1

    state = _load_state()
    key = _state_key(ctx)
    entry = state.get(key)
    if entry is None:
        exists_in_gcp = gcp.instance_describe(ctx) is not None
        suggestion = "--enroll (it already exists in GCP)" if exists_in_gcp else "--create"
        print(
            f"No local record of {ctx.instance} in {ctx.project}/{ctx.zone}. Run {suggestion} first.",
            file=out,
        )
        return 1

    status = gcp.instance_status(ctx)
    if status is None:
        print(
            f"Instance {ctx.instance} does not exist in GCP, though local state has a record of it "
            "(state file out of sync?). Run --create.",
            file=out,
        )
        return 1

    external_ip, ssh_ready = _boot_and_wait_for_ssh(ctx, status, out)
    if external_ip is None:
        print(f"{ctx.instance} is RUNNING but never got an external IP.", file=out)
        return 1

    public_ip = gcp.get_public_ipv4()
    _ensure_firewall_rule(ctx, name=entry["firewall_rule"], source_range=f"{public_ip}/32")

    if not ssh_ready:
        print(f"{ctx.instance} is RUNNING but never accepted an SSH connection.", file=out)
        return 1

    user = entry["user"]
    password = entry["password"]
    gcp.ssh_run(ctx, _remote_setup_command(user=user, password=password))
    gcp.ssh_run(ctx, _remote_launch_command())

    with _progress(out, "Waiting for the GNS3 server") as tick:
        ready = gcp.wait_for_http_ready(external_ip, SERVER_PORT, on_tick=tick)

    _take_gui_conf_backup_if_needed(state)
    gns3conf.patch_gui_conf(
        gns3conf.gns3_gui_config_path(),
        host=external_ip,
        port=SERVER_PORT,
        protocol="http",
        user=user,
        password=password,
    )
    # patch_gui_conf alone leaves the GUI pointed at "localhost": Preferences
    # -> Server -> Remote main server host is read from this separate,
    # INI-format file, not from gns3_gui.conf's Servers.remote_servers
    # (see gns3conf module docstring).
    gns3conf.patch_local_server_conf(
        gns3conf.gns3_local_server_conf_path(),
        host=external_ip,
        port=SERVER_PORT,
        protocol="http",
        user=user,
        password=password,
    )

    entry["external_ip"] = external_ip
    state[key] = entry
    _save_state(state)

    print("", file=out)
    print(f"Instance:  {ctx.instance} ({ctx.project}/{ctx.zone})", file=out)
    print(f"IP:        {external_ip}", file=out)
    print(f"Port:      {SERVER_PORT}", file=out)
    print(f"Username:  {user}", file=out)
    print(f"Password:  {password}", file=out)
    print(f"Web UI:    http://{external_ip}:{SERVER_PORT}/", file=out)
    print(
        f"Server:    {'ready' if ready else 'NOT responding yet — check ~/gns3server.log on the VM'}",
        file=out,
    )
    return 0 if ready else 1


def cmd_stop(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    if _refuse_if_gui_running(out):
        return 1

    # Restoring the GUI config is a purely local concern, independent of
    # the VM's cloud-side state, so it happens regardless of what's below.
    state = _load_state()
    _restore_gui_conf_if_held(state, out)
    _save_state(state)

    status = gcp.instance_status(ctx)
    if status is None:
        print(f"Instance {ctx.instance} does not exist in {ctx.project}/{ctx.zone}.", file=out)
        return 1
    if status == "TERMINATED":
        print(f"{ctx.instance} is already TERMINATED.", file=out)
        return 0

    print(f"Stopping {ctx.instance}...", file=out)  # gcloud shows its own progress here
    gcp.stop_instance(ctx)
    with _progress(out, "Waiting for instance to report TERMINATED") as tick:
        final_status = gcp.wait_for_status(ctx, "TERMINATED", on_tick=tick)
    print(f"{ctx.instance} is {final_status}.", file=out)
    return 0


def cmd_status(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    status = gcp.instance_status(ctx)
    if status is None:
        print(f"Instance {ctx.instance} does not exist in {ctx.project}/{ctx.zone}.", file=out)
        return 1

    state = _load_state()
    entry = state.get(_state_key(ctx))
    ip = gcp.instance_external_ip(ctx) if status == "RUNNING" else None

    print(f"Instance:    {ctx.instance} ({ctx.project}/{ctx.zone})", file=out)
    print(f"Status:      {status}", file=out)
    print(f"External IP: {ip or '(none)'}", file=out)
    if entry:
        print(f"Web UI:      {'http://' + ip + ':' + str(SERVER_PORT) + '/' if ip else '(VM not running)'}", file=out)
        print(f"Username:    {entry.get('user', '?')}", file=out)
        print(f"Password:    {entry.get('password', '?')}", file=out)
    else:
        print(
            "No local record for this instance (created outside this tool, or local state "
            "lost) — run --enroll to adopt it.",
            file=out,
        )
    return 0


_COMMANDS = {
    "scan": cmd_scan,
    "create": cmd_create,
    "enroll": cmd_enroll,
    "start": cmd_start,
    "refresh": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "scan"
    try:
        gcloud_exe = gcp.find_gcloud()
        ctx = _build_context(args, gcloud_exe, resolve_zone=command != "scan")
        kwargs = {"dry_run": args.dry_run} if command == "create" else {}
        return _COMMANDS[command](ctx, **kwargs)
    except gcp.GcloudNotFoundError as exc:
        print(f"error: {exc}\n", file=sys.stderr)
        setup_help.print_setup_instructions(sys.stderr)
        return 1
    except gcp.GcpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
