"""gcloud subprocess wrappers.

Every gcloud call goes through GcpContext.run so a config name (see
--gcloud-config) is applied consistently via CLOUDSDK_ACTIVE_CONFIG_NAME
rather than mutating the caller's active gcloud configuration — see
CLAUDE.md, "Account isolation" in the plan.

Structured fields (status, IP, ...) are always read back with
--format=json and parsed, never scraped from human-readable text: gcloud's
plain-text output isn't a stable contract, and "must verify afterwards
rather than trusting the exit code" (CLAUDE.md, apt fails atomically) is
the same principle applied to gcloud calls.
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


def find_gcloud() -> str:
    """Locate the gcloud executable. On Windows it's gcloud.cmd, not gcloud."""
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        raise GcpError(
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
) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    if config:
        env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = config

    result = subprocess.run(
        [gcloud_exe, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise GcpError(
            f"gcloud {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
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
    # Partial update: gcloud leaves every flag not passed here untouched
    # (CLAUDE.md, "gcloud firewall-rules update is partial").
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
    return resources.as_file(resources.files("gns3_2620_lab") / "provision.sh")


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
) -> dict:
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
        "--enable-nested-virtualization",
        f"--tags={','.join(tags)}",
    ]

    with ExitStack() as stack:
        if startup_script_path is None:
            startup_script_path = stack.enter_context(_bundled_provision_script_context())
        args.append(f"--metadata-from-file=startup-script={startup_script_path}")
        return ctx.run_json(args)


def start_instance(ctx: GcpContext) -> None:
    ctx.run(
        ["compute", "instances", "start", ctx.instance, "--project", ctx.project, "--zone", ctx.zone]
    )


def stop_instance(ctx: GcpContext) -> None:
    ctx.run(
        ["compute", "instances", "stop", ctx.instance, "--project", ctx.project, "--zone", ctx.zone]
    )


def wait_for_status(
    ctx: GcpContext,
    target_status: str,
    *,
    timeout: float = 180,
    poll_interval: float = 3,
    sleep=time.sleep,
    now=time.monotonic,
) -> str:
    deadline = now() + timeout
    status = instance_status(ctx)
    while status != target_status:
        if now() >= deadline:
            raise GcpError(
                f"timed out waiting for {ctx.instance} to reach {target_status} "
                f"(last seen: {status})"
            )
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
) -> str | None:
    deadline = now() + timeout
    ip = instance_external_ip(ctx)
    while ip is None:
        if now() >= deadline:
            return None
        sleep(poll_interval)
        ip = instance_external_ip(ctx)
    return ip


def ssh_run(ctx: GcpContext, command: str, *, timeout: float | None = 60) -> subprocess.CompletedProcess:
    # Each call is a fresh login shell (CLAUDE.md, §2.9) — callers that need
    # a group membership change to take effect must issue it as a separate
    # ssh_run call from the one that relies on it.
    return ctx.run(
        [
            "compute",
            "ssh",
            ctx.instance,
            "--project",
            ctx.project,
            "--zone",
            ctx.zone,
            "--command",
            command,
        ],
        timeout=timeout,
    )


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
        sleep(poll_interval)
