"""gcloud subprocess wrappers.

Every gcloud call goes through GcpContext.run so a config name (see
--gcloud-config) is applied consistently via CLOUDSDK_ACTIVE_CONFIG_NAME,
rather than mutating the caller's active gcloud configuration.

Structured fields (status, IP, ...) are always read back with --format=json
and parsed, never scraped from human-readable text: gcloud's plain-text
output isn't a stable contract, so results are verified rather than trusted
from the exit code alone.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import shutil
import socket
import subprocess
import time
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


class GcpError(RuntimeError):
    """A gcloud call failed or returned something we didn't expect."""


class GcloudNotFoundError(GcpError):
    """gcloud isn't on PATH — distinct from GcpError so callers can show
    full account/CLI setup instructions instead of a bare error line."""


def find_gcloud() -> str:
    """Locate the gcloud executable. On Windows it's gcloud.cmd, not gcloud."""
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        raise GcloudNotFoundError(
            "gcloud was not found on PATH. Install the Google Cloud SDK first."
        )
    return exe


def run(
    gcloud_exe: str,
    args: list,
    *,
    config: str | None = None,
    check: bool = True,
    timeout: float | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """capture=False lets gcloud's own stdout/stderr (including its native
    progress spinner on slow calls like `instances create`) go straight to
    the terminal instead of being buffered and replayed later. Only use it
    for calls whose output nothing downstream needs to parse — on failure,
    the user already saw gcloud's real error live, so the exception
    message doesn't repeat it."""
    import os

    env = os.environ.copy()
    if config:
        env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = config

    try:
        result = subprocess.run(
            [gcloud_exe, *args],
            env=env,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run raises instead of returning a CompletedProcess on a
        # timeout — check=False callers (e.g. wait_for_ssh_ready's poll
        # loop) expect a returncode to inspect on a failed attempt, not an
        # exception, so a slow attempt (sshd not answering yet, as opposed
        # to a fast "Connection refused") crashed the whole command instead
        # of being retried like any other failed attempt. Confirmed live.
        if check:
            raise GcpError(f"gcloud {' '.join(args)} timed out after {timeout}s") from exc
        return subprocess.CompletedProcess(
            [gcloud_exe, *args],
            returncode=124,
            stdout="" if capture else None,
            stderr=f"timed out after {timeout}s" if capture else None,
        )
    if check and result.returncode != 0:
        detail = f": {result.stderr.strip()}" if capture else " (see gcloud output above)"
        raise GcpError(f"gcloud {' '.join(args)} failed (exit {result.returncode}){detail}")
    return result


def run_json(gcloud_exe: str, args: list, *, config: str | None = None, timeout: float | None = None):
    result = run(gcloud_exe, [*args, "--format=json"], config=config, timeout=timeout)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GcpError(f"gcloud {' '.join(args)} did not return valid JSON: {exc}") from exc


@dataclass
class GcpContext:
    gcloud_exe: str
    project: str
    zone: str
    instance: str
    config_name: str | None = None
    # Set only for a VM enrolled with an explicit --ssh-user (cli.cmd_enroll):
    # gcloud never remembers a username across calls (confirmed — there's no
    # config property for it and metadata only controls who's *allowed* in,
    # not who gets picked), so every ssh_run call has to keep supplying it.
    ssh_user: str | None = None

    def run(self, args: list, **kwargs) -> subprocess.CompletedProcess:
        return run(self.gcloud_exe, args, config=self.config_name, **kwargs)

    def run_json(self, args: list, **kwargs):
        return run_json(self.gcloud_exe, args, config=self.config_name, **kwargs)


def get_active_project(gcloud_exe: str, config: str | None = None) -> str:
    result = run(gcloud_exe, ["config", "get-value", "project"], config=config)
    project = result.stdout.strip()
    if not project or project == "(unset)":
        raise GcpError(
            "No active GCP project. Pass --project, or set one with "
            "'gcloud config set project <PROJECT_ID>'."
        )
    return project


def get_active_zone(gcloud_exe: str, config: str | None = None) -> str:
    result = run(gcloud_exe, ["config", "get-value", "compute/zone"], config=config)
    zone = result.stdout.strip()
    if not zone or zone == "(unset)":
        raise GcpError(
            "No default zone configured. Pass --zone, or set one with "
            "'gcloud config set compute/zone <ZONE>'."
        )
    return zone


def instance_describe(ctx: GcpContext) -> dict | None:
    """Return the instance's describe JSON, or None if it doesn't exist."""
    result = ctx.run(
        [
            "compute",
            "instances",
            "describe",
            ctx.instance,
            "--project",
            ctx.project,
            "--zone",
            ctx.zone,
            "--format=json",
        ],
        check=False,
    )
    if result.returncode != 0:
        if "not found" in result.stderr.lower() or "NOT_FOUND" in result.stderr:
            return None
        raise GcpError(f"gcloud compute instances describe failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GcpError(f"instances describe did not return valid JSON: {exc}") from exc


def instance_status(ctx: GcpContext) -> str | None:
    """RUNNING / TERMINATED / STOPPING / ... or None if the instance doesn't exist."""
    info = instance_describe(ctx)
    return info["status"] if info else None


def instance_external_ip(ctx: GcpContext) -> str | None:
    info = instance_describe(ctx)
    if not info:
        return None
    try:
        access_configs = info["networkInterfaces"][0]["accessConfigs"]
    except (KeyError, IndexError):
        return None
    for cfg in access_configs:
        ip = cfg.get("natIP")
        if ip:
            return ip
    return None


def list_instances(gcloud_exe: str, project: str, *, config: str | None = None) -> list:
    """All compute instances in the project, across every zone — deliberately
    unfiltered (not just ones tagged/created by this tool), since the point
    is helping a user find a VM whose name they don't remember, including
    one nobody created through this wrapper."""
    return run_json(gcloud_exe, ["compute", "instances", "list", "--project", project], config=config)


def instance_zone_name(info: dict) -> str:
    """instances.list/describe report zone as a full resource URL
    (".../zones/us-west1-b"); callers just want the bare name."""
    return info["zone"].rsplit("/", 1)[-1]


def create_firewall_rule(
    ctx: GcpContext,
    *,
    name: str,
    tags: list,
    source_range: str,
    ports: list,
) -> None:
    ctx.run(
        [
            "compute",
            "firewall-rules",
            "create",
            name,
            "--project",
            ctx.project,
            "--direction=INGRESS",
            "--action=ALLOW",
            f"--rules={','.join(ports)}",
            f"--source-ranges={source_range}",
            f"--target-tags={','.join(tags)}",
        ]
    )


def update_firewall_source_range(ctx: GcpContext, *, name: str, source_range: str) -> None:
    # Partial update: gcloud leaves every flag not passed here untouched.
    ctx.run(
        [
            "compute",
            "firewall-rules",
            "update",
            name,
            "--project",
            ctx.project,
            f"--source-ranges={source_range}",
        ]
    )


def firewall_rule_exists(ctx: GcpContext, name: str) -> bool:
    result = ctx.run(
        ["compute", "firewall-rules", "describe", name, "--project", ctx.project],
        check=False,
    )
    return result.returncode == 0


def _bundled_provision_script_context():
    """Context manager yielding a real filesystem path to the bundled
    provision.sh, whether running from an installed wheel or an editable
    checkout."""
    return resources.as_file(resources.files("gns3_cloud_lab") / "provision.sh")


def create_instance(
    ctx: GcpContext,
    *,
    machine_type: str = "n2-standard-4",
    image_family: str = "ubuntu-2404-lts-amd64",
    image_project: str = "ubuntu-os-cloud",
    disk_size_gb: int = 30,
    disk_type: str = "pd-balanced",
    tags: list,
    startup_script_path: Path | None = None,
) -> None:
    # capture=False (no --format=json either, since nothing parses the
    # result): this call alone can take 10-60s, and gcloud has its own
    # native progress spinner for it — better to let that show live than
    # buffer it silently the whole time.
    args = [
        "compute",
        "instances",
        "create",
        ctx.instance,
        "--project",
        ctx.project,
        "--zone",
        ctx.zone,
        f"--machine-type={machine_type}",
        f"--image-family={image_family}",
        f"--image-project={image_project}",
        f"--boot-disk-size={disk_size_gb}GB",
        f"--boot-disk-type={disk_type}",
        # Requires machine_type in N1/N2/C2 — nested virtualization needs a
        # Haswell+ host CPU, unavailable on E2/N2D/AMD/Arm/memory-optimized.
        "--enable-nested-virtualization",
        f"--tags={','.join(tags)}",
    ]

    with ExitStack() as stack:
        if startup_script_path is None:
            startup_script_path = stack.enter_context(_bundled_provision_script_context())
        args.append(f"--metadata-from-file=startup-script={startup_script_path}")
        ctx.run(args, capture=False)


def start_instance(ctx: GcpContext) -> None:
    ctx.run(
        ["compute", "instances", "start", ctx.instance, "--project", ctx.project, "--zone", ctx.zone],
        capture=False,
    )


def stop_instance(ctx: GcpContext) -> None:
    ctx.run(
        ["compute", "instances", "stop", ctx.instance, "--project", ctx.project, "--zone", ctx.zone],
        capture=False,
    )


def wait_for_status(
    ctx: GcpContext,
    target_status: str,
    *,
    timeout: float = 180,
    poll_interval: float = 3,
    sleep=time.sleep,
    now=time.monotonic,
    on_tick=lambda: None,
) -> str:
    deadline = now() + timeout
    status = instance_status(ctx)
    while status != target_status:
        if now() >= deadline:
            raise GcpError(
                f"timed out waiting for {ctx.instance} to reach {target_status} "
                f"(last seen: {status})"
            )
        on_tick()
        sleep(poll_interval)
        status = instance_status(ctx)
    return status


def wait_for_external_ip(
    ctx: GcpContext,
    *,
    timeout: float = 60,
    poll_interval: float = 3,
    sleep=time.sleep,
    now=time.monotonic,
    on_tick=lambda: None,
) -> str | None:
    deadline = now() + timeout
    ip = instance_external_ip(ctx)
    while ip is None:
        if now() >= deadline:
            return None
        on_tick()
        sleep(poll_interval)
        ip = instance_external_ip(ctx)
    return ip


def _ssh_target(ctx: GcpContext) -> str:
    """USER@INSTANCE if a pinned ssh_user is set, else the bare instance name
    gcloud has always used (which resolves to whatever username gcloud's own
    default-username logic picks — see the comment on GcpContext.ssh_user)."""
    return f"{ctx.ssh_user}@{ctx.instance}" if ctx.ssh_user else ctx.instance


def ssh_run(
    ctx: GcpContext, command: str, *, check: bool = True, timeout: float | None = 60
) -> subprocess.CompletedProcess:
    # Each call is a fresh login shell — callers that need a group
    # membership change to take effect must issue it as a separate ssh_run
    # call from the one that relies on it.
    #
    # gcloud on Windows is gcloud.cmd, a batch file. Windows can't launch a
    # batch file directly, so CreateProcess silently relaunches it through
    # `cmd.exe /c`, which rebuilds and re-parses a single command line out
    # of our whole argv. A single-line argument survives that round trip
    # (confirmed: spaces, double quotes, $(), pipes, and semicolons all
    # arrive at the VM intact) but a real newline does not — cmd.exe has
    # no way to represent one, and the argument comes out scrambled,
    # starting with a fragment of cmd.exe's own COMSPEC path. Reproduced
    # live against a real VM from the Windows side; the fix below (upload
    # the script, run it by name) was verified end to end the same way.
    # Only the batch shim needs this: a real gcloud binary (Linux/macOS)
    # receives argv directly with no relaunch, so multi-line commands
    # already reach it unmodified — that path is untouched below.
    if "\n" in command and ctx.gcloud_exe.lower().endswith((".cmd", ".bat")):
        return _ssh_run_via_upload(ctx, command, check=check, timeout=timeout)
    return ctx.run(
        [
            "compute",
            "ssh",
            _ssh_target(ctx),
            "--project",
            ctx.project,
            "--zone",
            ctx.zone,
            "--command",
            command,
        ],
        check=check,
        timeout=timeout,
    )


# Fixed name, not a per-call temp name: each ssh_run call is its own
# gcloud invocation with nothing to correlate a random name back to, and
# the upload-then-run-then-delete sequence below always cleans it up
# before returning, so nothing accumulates.
_REMOTE_UPLOAD_NAME = ".gclab_cmd.sh"


def _ssh_run_via_upload(
    ctx: GcpContext, command: str, *, check: bool, timeout: float | None
) -> subprocess.CompletedProcess:
    """The Windows-only path ssh_run switches to for multi-line commands —
    see the comment there.

    newline="\\n" on the local temp file forces LF line endings regardless
    of the host's text-mode default: a CRLF would put a trailing \\r on
    the heredoc delimiter line inside _remote_setup_command's script, so
    it no longer matches the opening `<<'...'` byte-for-byte and the
    heredoc never closes.
    """
    import os
    import tempfile

    target = _ssh_target(ctx)
    fd, local_path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", newline="\n") as f:
            f.write(command)
        # gcloud compute scp accepts the same [USER@]INSTANCE: prefix as
        # compute ssh (confirmed against gcloud's own reference docs) — the
        # upload has to land in the pinned user's home too, or the script
        # gets written to one account and run as another.
        ctx.run(
            [
                "compute",
                "scp",
                local_path,
                f"{target}:{_REMOTE_UPLOAD_NAME}",
                "--project",
                ctx.project,
                "--zone",
                ctx.zone,
            ],
            timeout=timeout,
        )
    finally:
        Path(local_path).unlink(missing_ok=True)
    # `; ec=$?; rm -f ...; exit $ec`, not `&&`: the remote temp file must
    # be cleaned up and the *script's* exit code must survive even when
    # the script itself fails (e.g. _remote_setup_command's `exit 1` on a
    # missing group) — `cmd1; cmd2` reports cmd2's status, so cmd1's has
    # to be captured before cmd2 (the cleanup) can overwrite it. This
    # whole line is itself single-line, so it's exactly the kind of
    # command already confirmed safe to pass straight through --command.
    return ctx.run(
        [
            "compute",
            "ssh",
            target,
            "--project",
            ctx.project,
            "--zone",
            ctx.zone,
            "--command",
            f"bash {_REMOTE_UPLOAD_NAME}; ec=$?; rm -f {_REMOTE_UPLOAD_NAME}; exit $ec",
        ],
        check=check,
        timeout=timeout,
    )


def wait_for_ssh_ready(
    ctx: GcpContext,
    *,
    timeout: float = 120,
    poll_interval: float = 5,
    sleep=time.sleep,
    now=time.monotonic,
    on_tick=lambda: None,
) -> bool:
    """GCE reporting an instance RUNNING only means it started booting, not
    that sshd is accepting connections yet. Poll a trivial remote command
    instead of assuming SSH is ready as soon as an IP is."""
    deadline = now() + timeout
    while True:
        result = ssh_run(ctx, "true", check=False, timeout=15)
        if result.returncode == 0:
            return True
        if now() >= deadline:
            return False
        on_tick()
        sleep(poll_interval)


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that resolves and connects over IPv4 only.

    curl -4's equivalent: an IPv6 result would be rejected by GCP as an
    invalid /32 CIDR when used as a firewall source range.
    """

    def connect(self):
        addr_info = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        sock = socket.socket(addr_info[0][0], socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(addr_info[0][4])
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def get_public_ipv4(
    service_host: str = "api.ipify.org",
    path: str = "/?format=text",
    timeout: float = 10,
    connection_cls=_IPv4HTTPSConnection,
) -> str:
    conn = connection_cls(service_host, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            raise GcpError(f"public IP lookup returned HTTP {resp.status}")
        ip = resp.read().decode().strip()
    finally:
        conn.close()

    try:
        ipaddress.IPv4Address(ip)
    except ValueError as exc:
        raise GcpError(f"public IP lookup did not return an IPv4 address: {ip!r}") from exc
    return ip


def wait_for_http_ready(
    host: str,
    port: int,
    *,
    path: str = "/v2/version",
    timeout: float = 120,
    poll_interval: float = 3,
    sleep=time.sleep,
    now=time.monotonic,
    connection_cls=http.client.HTTPConnection,
    on_tick=lambda: None,
) -> bool:
    deadline = now() + timeout
    while True:
        try:
            conn = connection_cls(host, port, timeout=5)
            try:
                conn.request("GET", path)
                resp = conn.getresponse()
                if resp.status < 500:
                    return True
            finally:
                conn.close()
        except OSError:
            pass
        if now() >= deadline:
            return False
        on_tick()
        sleep(poll_interval)
