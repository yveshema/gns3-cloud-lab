#!/usr/bin/env python3
"""Uninstall the gns3-cloud-lab CLI. Safe to run whether or not it's installed.

Author: Yves R. Shema <yshema@bcit.ca>
Co-Authored-By: Claude <noreply@anthropic.com>
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("uv isn't on PATH — nothing to uninstall.")
        return 0

    # Imported from source directly, same as install.py, rather than
    # assuming the installed console script is still on this process's PATH.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from gns3_cloud_lab import alias

    alias.remove_if_ours(uv)
    # uv already prints its own message when the tool wasn't installed;
    # either way there's nothing left to do afterward.
    subprocess.run([uv, "tool", "uninstall", "gns3-cloud-lab"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
