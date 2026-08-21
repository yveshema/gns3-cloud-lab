"""Create/remove the `gclab` convenience alias for gns3-cloud-lab.

Only ever touches the name if nothing else on the machine already provides
it (create), or if the existing alias clearly points at this tool's own
installed command (remove) — never shadows or deletes something this tool
didn't create itself.

No shell aliases, no editing .bashrc/.profile/PowerShell profiles: the
alias is a real symlink (Linux/macOS) or a tiny generated .cmd shim
(Windows — real symlinks there need Developer Mode or admin rights, and
uv's own tool executables are copied rather than symlinked on Windows for
the same reason), placed in the exact directory `uv tool install` already
put the real command in, discovered via `uv tool dir --bin` rather than
assumed — so it's on PATH under whatever conditions the real command
already is, nothing extra to set up.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ALIAS_NAME = "gclab"
REAL_COMMAND = "gns3-cloud-lab"


def _bin_dir(uv: str) -> Path | None:
    result = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def _target_name() -> str:
    return f"{REAL_COMMAND}.exe" if sys.platform == "win32" else REAL_COMMAND


def _alias_path(bin_dir: Path) -> Path:
    return bin_dir / (f"{ALIAS_NAME}.cmd" if sys.platform == "win32" else ALIAS_NAME)


def create_if_available(uv: str, out=None) -> None:
    out = out or sys.stdout
    if shutil.which(ALIAS_NAME):
        return  # something already provides this name -- never shadow it

    bin_dir = _bin_dir(uv)
    if bin_dir is None:
        return
    target = bin_dir / _target_name()
    if not target.exists():
        return

    alias_path = _alias_path(bin_dir)
    try:
        if sys.platform == "win32":
            alias_path.write_text(f'@"%~dp0{target.name}" %*\r\n')
        else:
            alias_path.symlink_to(target.name)  # relative: same directory as the real command
    except OSError:
        return  # best-effort only -- the real command still works either way

    print(f"Also added '{ALIAS_NAME}' as a shorter alias for '{REAL_COMMAND}'.", file=out)


def remove_if_ours(uv: str) -> None:
    bin_dir = _bin_dir(uv)
    if bin_dir is None:
        return
    alias_path = _alias_path(bin_dir)

    try:
        if sys.platform == "win32":
            if not alias_path.exists():
                return
            is_ours = _target_name() in alias_path.read_text()
        else:
            if not alias_path.is_symlink():
                return
            is_ours = os.readlink(alias_path) == _target_name()
        if is_ours:
            alias_path.unlink()
    except OSError:
        pass
