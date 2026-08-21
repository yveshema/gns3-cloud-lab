import io
import os

import pytest

from gns3_cloud_lab import alias


def _fake_dir_result(bin_dir, returncode=0):
    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = f"{bin_dir}\n" if returncode == 0 else ""
    return result


# ---------------------------------------------------------------------------
# create_if_available
# ---------------------------------------------------------------------------


def test_create_skips_when_something_already_provides_the_name(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.shutil, "which", lambda name: "/usr/bin/gclab")
    called = []
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: called.append(True))
    alias.create_if_available("uv")
    assert called == []  # never even asked uv where its bin dir is


def test_create_makes_a_symlink_on_unix(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.sys, "platform", "linux")
    monkeypatch.setattr(alias.shutil, "which", lambda name: None)
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))
    (tmp_path / "gns3-cloud-lab").write_text("#!/bin/sh\n")

    out = io.StringIO()
    alias.create_if_available("uv", out=out)

    link = tmp_path / "gclab"
    assert link.is_symlink()
    assert os.readlink(link) == "gns3-cloud-lab"  # relative, same directory
    assert "gclab" in out.getvalue()


def test_create_writes_a_cmd_shim_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.sys, "platform", "win32")
    monkeypatch.setattr(alias.shutil, "which", lambda name: None)
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))
    (tmp_path / "gns3-cloud-lab.exe").write_text("")

    alias.create_if_available("uv")

    shim = tmp_path / "gclab.cmd"
    assert shim.exists()
    assert "gns3-cloud-lab.exe" in shim.read_text()


def test_create_does_nothing_if_target_executable_missing(monkeypatch, tmp_path):
    # uv reported a bin dir, but the real command isn't in it (unexpected
    # naming, or called before install actually succeeded) -- don't create
    # an alias pointing at nothing.
    monkeypatch.setattr(alias.sys, "platform", "linux")
    monkeypatch.setattr(alias.shutil, "which", lambda name: None)
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))

    alias.create_if_available("uv")
    assert not (tmp_path / "gclab").exists()


def test_create_does_nothing_if_uv_tool_dir_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.shutil, "which", lambda name: None)
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path, returncode=1))

    alias.create_if_available("uv")  # must not raise
    assert not (tmp_path / "gclab").exists()


# ---------------------------------------------------------------------------
# remove_if_ours
# ---------------------------------------------------------------------------


def test_remove_deletes_a_symlink_pointing_at_our_command(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.sys, "platform", "linux")
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))
    (tmp_path / "gclab").symlink_to("gns3-cloud-lab")

    alias.remove_if_ours("uv")
    assert not (tmp_path / "gclab").exists()


def test_remove_leaves_a_symlink_pointing_elsewhere_alone(monkeypatch, tmp_path):
    # Regression test: never delete a gclab this tool didn't create, even
    # if it happens to already exist by the time uninstall runs.
    monkeypatch.setattr(alias.sys, "platform", "linux")
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))
    (tmp_path / "gclab").symlink_to("/usr/bin/true")

    alias.remove_if_ours("uv")
    assert (tmp_path / "gclab").is_symlink()


def test_remove_noop_when_no_alias_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.sys, "platform", "linux")
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))

    alias.remove_if_ours("uv")  # must not raise


def test_remove_deletes_our_cmd_shim_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.sys, "platform", "win32")
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))
    (tmp_path / "gclab.cmd").write_text('@"%~dp0gns3-cloud-lab.exe" %*\r\n')

    alias.remove_if_ours("uv")
    assert not (tmp_path / "gclab.cmd").exists()


def test_remove_leaves_unrelated_cmd_shim_alone_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(alias.sys, "platform", "win32")
    monkeypatch.setattr(alias.subprocess, "run", lambda *a, **k: _fake_dir_result(tmp_path))
    (tmp_path / "gclab.cmd").write_text('@"%~dp0something-else.exe" %*\r\n')

    alias.remove_if_ours("uv")
    assert (tmp_path / "gclab.cmd").exists()
