"""--create --start --stop --status.

The wrapper's actual reason to exist (CLAUDE.md): the VM's external IP and
the student's public IP both change every session, in opposite directions.
--start is the only command that refreshes both the firewall source range
and the GUI config; --create and --stop are comparatively simple.

WARNING — the remote gns3_server.conf format written in _remote_setup_command
is a guess (JSON, mirroring gns3_controller.conf's known format), never
confirmed against a real server. This is the single biggest unvalidated
assumption in the wrapper; see CLAUDE.md, "Needs live-VM validation".
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from . import gcp, gns3conf

DEFAULT_INSTANCE = "gns3-lab"
DEFAULT_ZONE = "us-west1-b"
DEFAULT_MACHINE_TYPE = "n2-standard-4"
DEFAULT_TAG = "gns3"
FIREWALL_PORTS = ["tcp:3080", "tcp:5000-5020"]
SERVER_PORT = 3080
GUI_USER = "admin"
CONSOLE_PORT_START = 5000
CONSOLE_PORT_END = 5020


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gns3-2620-lab",
        description="Per-student GCP fallback for BCIT networking-2620 GNS3 labs.",
    )
    parser.add_argument(
        "--project", default=None, help="GCP project (default: active gcloud configuration's project)"
    )
    parser.add_argument(
        "--instance", default=DEFAULT_INSTANCE, help=f"VM instance name (default: {DEFAULT_INSTANCE})"
    )
    parser.add_argument("--zone", default=DEFAULT_ZONE, help=f"GCP zone (default: {DEFAULT_ZONE})")
    parser.add_argument(
        "--gcloud-config", default=None, help="gcloud configuration name to use for this run"
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="Create the firewall rule and VM")
    sub.add_parser("start", help="Start the VM and refresh firewall/GUI config")
    sub.add_parser("stop", help="Stop the VM")
    sub.add_parser("status", help="Print VM and server status")
    return parser


def _firewall_name(instance: str) -> str:
    return f"{instance}-gns3"


def _build_context(args, gcloud_exe: str) -> gcp.GcpContext:
    project = args.project or gcp.get_active_project(gcloud_exe, args.gcloud_config)
    return gcp.GcpContext(
        gcloud_exe=gcloud_exe,
        project=project,
        zone=args.zone,
        instance=args.instance,
        config_name=args.gcloud_config,
    )


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


def _remote_setup_command(*, user: str, password: str) -> str:
    server_conf = {
        "Server": {
            "host": "0.0.0.0",
            "port": SERVER_PORT,
            "auth": True,
            "user": user,
            "password": password,
            "console_start_port_range": CONSOLE_PORT_START,
            "console_end_port_range": CONSOLE_PORT_END,
        }
    }
    conf_json = json.dumps(server_conf, indent=4)
    # A single-quoted heredoc delimiter disables shell expansion inside the
    # body, so the generated password is safe here even if it contains
    # characters like $ or ` (secrets.token_urlsafe never emits a literal
    # single quote, so the delimiter itself can't be broken out of).
    return (
        "set -e; "
        'id -nG "$(whoami)" | grep -qw kvm && id -nG "$(whoami)" | grep -qw docker '
        '|| sudo usermod -aG kvm,docker "$(whoami)"; '
        "mkdir -p ~/.config/GNS3/2.2; "
        "cat > ~/.config/GNS3/2.2/gns3_server.conf <<'GNS3_SERVER_CONF_EOF'\n"
        f"{conf_json}\n"
        "GNS3_SERVER_CONF_EOF"
    )


def _remote_launch_command() -> str:
    # Separate --command call from _remote_setup_command: group membership
    # from usermod only applies to a new login, and every --command
    # invocation is a fresh one (CLAUDE.md, §2.9).
    return (
        "pgrep -f gns3server >/dev/null 2>&1 "
        "|| setsid nohup gns3server </dev/null >~/gns3server.log 2>&1 &"
    )


def cmd_create(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    if gcp.instance_describe(ctx) is not None:
        print(
            f"Instance {ctx.instance} already exists in {ctx.project}/{ctx.zone}. Nothing to create.",
            file=out,
        )
        return 0

    password = secrets.token_urlsafe(18)
    public_ip = gcp.get_public_ipv4()
    fw_name = _firewall_name(ctx.instance)
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


def cmd_start(ctx: gcp.GcpContext, out=sys.stdout) -> int:
    state = _load_state()
    key = _state_key(ctx)
    entry = state.get(key)
    if entry is None:
        print(
            f"No local record of {ctx.instance} in {ctx.project}/{ctx.zone}. Run --create first.",
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

    if status != "RUNNING":
        gcp.start_instance(ctx)
        gcp.wait_for_status(ctx, "RUNNING")

    external_ip = gcp.wait_for_external_ip(ctx)
    if external_ip is None:
        print(f"{ctx.instance} is RUNNING but never got an external IP.", file=out)
        return 1

    public_ip = gcp.get_public_ipv4()
    _ensure_firewall_rule(ctx, name=entry["firewall_rule"], source_range=f"{public_ip}/32")

    user = entry["user"]
    password = entry["password"]
    gcp.ssh_run(ctx, _remote_setup_command(user=user, password=password))
    gcp.ssh_run(ctx, _remote_launch_command())

    ready = gcp.wait_for_http_ready(external_ip, SERVER_PORT)

    gns3conf.patch_gui_conf(
        gns3conf.gns3_gui_config_path(),
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
    status = gcp.instance_status(ctx)
    if status is None:
        print(f"Instance {ctx.instance} does not exist in {ctx.project}/{ctx.zone}.", file=out)
        return 1
    if status == "TERMINATED":
        print(f"{ctx.instance} is already TERMINATED.", file=out)
        return 0

    gcp.stop_instance(ctx)
    final_status = gcp.wait_for_status(ctx, "TERMINATED")
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
        print("No local record for this instance (created outside this tool, or local state lost).", file=out)
    return 0


_COMMANDS = {
    "create": cmd_create,
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        gcloud_exe = gcp.find_gcloud()
        ctx = _build_context(args, gcloud_exe)
        return _COMMANDS[args.command](ctx)
    except gcp.GcpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
