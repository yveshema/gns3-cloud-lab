"""Locate and patch GNS3 client config, and the wrapper's own local state,
per platform.

GNS3's own config-path rule was only confirmed empirically on the Linux
*server* side (CLAUDE.md: "config directory is derived from expanduser('~')
of the user running the process ... XDG_CONFIG_HOME is not honoured").
Windows and macOS client paths below follow GNS3's documented layout, not
anything verified in this repo — see "Needs live-VM validation" in
CLAUDE.md.

gns3_gui.conf is JSON (GNS3 2.x), not the older INI/QSettings format. The
exact schema for a remote server entry was not confirmed against a real
client install; upsert_remote_server is a best-effort patch that preserves
every other key so a wrong guess here doesn't corrupt the rest of a
student's config.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path


def app_config_dir(app_name: str) -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            raise RuntimeError("APPDATA is not set")
        return Path(base) / app_name
    return Path.home() / ".config" / app_name


def gns3_gui_config_dir() -> Path:
    return app_config_dir("GNS3") / "2.2"


def gns3_gui_config_path() -> Path:
    return gns3_gui_config_dir() / "gns3_gui.conf"


def wrapper_state_dir() -> Path:
    return app_config_dir("gns3-2620-lab")


def wrapper_state_path() -> Path:
    return wrapper_state_dir() / "state.json"


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
    """Return a copy of conf with the gns3-2620-lab remote server entry
    added or updated in place, leaving every other key untouched.

    Entries are matched by "user" — good enough as long as the wrapper only
    ever manages a single named student account ("admin") on the lab VM,
    which is the current design (CLAUDE.md, credentials are wrapper-owned).
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
