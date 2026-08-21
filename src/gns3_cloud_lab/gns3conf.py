"""Locate and patch GNS3 client config, and the wrapper's own local state,
per platform.

Config directory (app_config_dir): on Windows, %APPDATA%. On Linux/macOS,
$XDG_CONFIG_HOME if set, else expanduser("~")/.config — mirroring
gns3-gui's own algorithm, so patched files land where the GUI actually
reads from. GNS3 itself lands in .../GNS3/2.2/. This path is shared by
every GNS3 install on a machine regardless of install method, and writing
straight to it (unprofiled) is intentional: it's the one GNS3 client the
user actually runs, and the whole point of `start` is to configure that
client.

The *remote* gns3_server.conf cli.py writes over SSH onto the VM is
unrelated to this: it lives on a different machine, and gns3-server's own
config-path algorithm does not check XDG_CONFIG_HOME at all.

Two client-side config files live in the same directory, and must not be
confused with each other (nor with that remote gns3_server.conf — three
files, three different machines/formats, one shared naming pattern):

- gns3_gui.conf — JSON. Servers.remote_servers is an *additional* compute
  servers list (assignable per-node), not what the GUI itself connects to.
- gns3_server.conf — INI (configparser), section [Server]. THIS is what
  Preferences -> Server -> "Remote main server host" actually reads
  (LocalServer.localServerSettings(), via LocalServerConfig in the real
  gns3-gui source) when "Enable local server" is unchecked (auto_start =
  False).

References (each verified directly against the source at the URL given,
not carried over from memory):
- gns3/local_config.py — configDirectory(), isMainGui():
  https://github.com/GNS3/gns3-gui/blob/c776d4ef3764b3db8ed21c8e4eedbd6cd388e5dc/gns3/local_config.py#L141-L179
  https://github.com/GNS3/gns3-gui/blob/c776d4ef3764b3db8ed21c8e4eedbd6cd388e5dc/gns3/local_config.py#L478-L520
- gns3/local_server_config.py, gns3/local_server.py — LocalServerConfig,
  localServerSettings(), auto_start gating local vs. remote:
  https://github.com/GNS3/gns3-gui/blob/c776d4ef3764b3db8ed21c8e4eedbd6cd388e5dc/gns3/local_server_config.py#L27-L53
  https://github.com/GNS3/gns3-gui/blob/c776d4ef3764b3db8ed21c8e4eedbd6cd388e5dc/gns3/local_server.py#L212-L296
- gns3/compute_manager.py — remoteComputes():
  https://github.com/GNS3/gns3-gui/blob/c776d4ef3764b3db8ed21c8e4eedbd6cd388e5dc/gns3/compute_manager.py#L185-L190
- gns3server/config.py — server-side config path, no XDG_CONFIG_HOME check
  (pinned to the gns3-server version provision.sh installs):
  https://github.com/GNS3/gns3-server/blob/v2.2.61/gns3server/config.py#L95-L107

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""

from __future__ import annotations

import configparser
import json
import os
import platform
from pathlib import Path

import psutil


def app_config_dir(app_name: str) -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            raise RuntimeError("APPDATA is not set")
        return Path(base) / app_name
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / app_name


def gns3_gui_config_dir() -> Path:
    return app_config_dir("GNS3") / "2.2"


def gns3_gui_config_path() -> Path:
    return gns3_gui_config_dir() / "gns3_gui.conf"


def gns3_gui_pid_path() -> Path:
    return gns3_gui_config_dir() / "gns3_gui.pid"


def gns3_local_server_conf_path() -> Path:
    """The client-side gns3_server.conf (INI) — see module docstring. Not
    to be confused with the JSON file cli.py writes over SSH onto the VM,
    which shares the same filename but lives on a different machine
    entirely and uses a different format."""
    return gns3_gui_config_dir() / "gns3_server.conf"


def wrapper_state_dir() -> Path:
    return app_config_dir("gns3-cloud-lab")


def wrapper_state_path() -> Path:
    return wrapper_state_dir() / "state.json"


def wrapper_gui_conf_backup_path() -> Path:
    return wrapper_state_dir() / "gns3_gui.conf.bak"


def wrapper_local_server_conf_backup_path() -> Path:
    return wrapper_state_dir() / "gns3_server.conf.bak"


def gui_is_running(pid_path: Path | None = None) -> bool:
    """Mirrors GNS3 GUI's own isMainGui() check (gns3-gui's local_config.py):
    the GUI writes its own PID to gns3_gui.pid next to gns3_gui.conf and
    never deletes it, so existence alone doesn't mean it's still running —
    the PID must also belong to a live process that looks like GNS3/Python.
    """
    if pid_path is None:
        pid_path = gns3_gui_pid_path()
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False
    return "gns3" in process.name().lower() or "python" in process.name().lower()


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return {}
    return json.loads(content)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=True)
        f.write("\n")


def upsert_remote_server(
    conf: dict,
    *,
    host: str,
    port: int,
    protocol: str,
    user: str,
    password: str,
) -> dict:
    """Return a copy of conf with the gns3-cloud-lab remote server entry
    added or updated in place, leaving every other key untouched.

    Entries are matched by "user" — good enough as long as the wrapper only
    ever manages a single named account ("admin") on the lab VM.
    """
    conf = dict(conf)
    servers = dict(conf.get("Servers", {}))
    remote_servers = list(servers.get("remote_servers", []))

    entry = {
        "host": host,
        "port": port,
        "protocol": protocol,
        "user": user,
        "password": password,
    }

    for i, existing in enumerate(remote_servers):
        if existing.get("user") == user:
            merged = dict(existing)
            merged.update(entry)
            remote_servers[i] = merged
            break
    else:
        remote_servers.append(entry)

    servers["remote_servers"] = remote_servers
    conf["Servers"] = servers
    return conf


def patch_gui_conf(
    path: Path,
    *,
    host: str,
    port: int,
    protocol: str,
    user: str,
    password: str,
) -> dict:
    conf = load_json(path)
    conf = upsert_remote_server(
        conf, host=host, port=port, protocol=protocol, user=user, password=password
    )
    save_json(path, conf)
    return conf


def patch_local_server_conf(
    path: Path,
    *,
    host: str,
    port: int,
    protocol: str,
    user: str,
    password: str,
) -> None:
    """Set the client-side [Server] section that Preferences -> Server ->
    "Remote main server host" actually reads (see module docstring) —
    auto_start=False is what makes the GUI treat this as its main server
    instead of trying to run/use a local one. Every other key (paths, port
    ranges, ...) is left alone if present; GNS3's own loadSettings() fills
    in anything missing with its own defaults on next load, same as it
    already does for a config file GNS3 itself has never fully populated.
    """
    config = configparser.RawConfigParser()
    if path.exists():
        config.read(path, encoding="utf-8")
    if not config.has_section("Server"):
        config.add_section("Server")
    config.set("Server", "host", host)
    config.set("Server", "port", str(port))
    config.set("Server", "protocol", protocol)
    config.set("Server", "user", user)
    config.set("Server", "password", password)
    config.set("Server", "auth", "True")
    config.set("Server", "auto_start", "False")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        config.write(f)
