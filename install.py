#!/usr/bin/env python3
"""Install (or reinstall) the gns3-cloud-lab CLI.

The only prerequisite is Python 3. If `uv` isn't already on PATH, this
bootstraps a private copy via the official installer (curl on Mac/Linux,
PowerShell on Windows) into a per-user cache directory, using
UV_UNMANAGED_INSTALL so it doesn't touch shell startup files or register
for self-updates — this is a one-shot bootstrap, not a "real" uv install.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BOOTSTRAP_DIR = Path.home() / ".gns3-cloud-lab" / "uv-bootstrap"


def _bootstrapped_uv_path() -> Path:
    return BOOTSTRAP_DIR / ("uv.exe" if sys.platform == "win32" else "uv")


def locate_uv() -> str | None:
    """Return a usable uv command if one's already available (on PATH, or a
    previously bootstrapped copy), without installing anything."""
    found = shutil.which("uv")
    if found:
        return found
    local_uv = _bootstrapped_uv_path()
    return str(local_uv) if local_uv.exists() else None


def bootstrap_uv() -> str:
    """Download and install a private copy of uv. Only called when
    locate_uv() found nothing."""
    print("uv not found — downloading it (one-time setup)...")
    BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
    env = {"UV_INSTALL_DIR": str(BOOTSTRAP_DIR), "UV_UNMANAGED_INSTALL": str(BOOTSTRAP_DIR)}
    if sys.platform == "win32":
        command = (
            "powershell -ExecutionPolicy ByPass -c "
            '"irm https://astral.sh/uv/install.ps1 | iex"'
        )
    else:
        command = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    result = subprocess.run(command, shell=True, env={**os.environ, **env})
    local_uv = _bootstrapped_uv_path()
    if result.returncode != 0 or not local_uv.exists():
        sys.exit("Could not install uv automatically. Install it yourself (https://docs.astral.sh/uv/) and try again.")
    return str(local_uv)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without changing anything"
    )
    args = parser.parse_args()

    uv = locate_uv()
    if uv:
        print(f"Using uv: {uv}")
    elif args.dry_run:
        # Nothing more to check without actually running the installer —
        # this only confirms the bootstrap branch would be taken.
        print(f"uv not found on this machine — would download it into {BOOTSTRAP_DIR}")
        uv = str(_bootstrapped_uv_path())
    else:
        uv = bootstrap_uv()

    project_dir = Path(__file__).resolve().parent
    if args.dry_run:
        print(f"Would run: {uv} tool install --force --no-cache .  (in {project_dir})")
        return 0

    # --no-cache alongside --force: --force alone can silently reinstall a
    # stale cached build with no "Building..." line in the output.
    result = subprocess.run([uv, "tool", "install", "--force", "--no-cache", "."], cwd=project_dir)
    if result.returncode == 0:
        print("\nInstalled. Run 'gns3-cloud-lab -h' to get started.")
        # Imported from source directly rather than assuming the just-installed
        # console script is already on this process's PATH.
        sys.path.insert(0, str(project_dir / "src"))
        from gns3_cloud_lab import alias, setup_help

        alias.create_if_available(uv)
        print()
        setup_help.print_disclaimer()
        print()
        setup_help.print_setup_instructions()
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
