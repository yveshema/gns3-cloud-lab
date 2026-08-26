import configparser
import json

import psutil
import pytest

from gns3_cloud_lab import gns3conf


@pytest.fixture(autouse=True)
def _no_ambient_xdg_config_home(monkeypatch):
    # So these tests are deterministic regardless of whether XDG_CONFIG_HOME
    # happens to be set in whatever environment runs them; the one test that
    # needs it set does so explicitly, overriding this.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


# ---------------------------------------------------------------------------
# app_config_dir / path helpers
# ---------------------------------------------------------------------------


def test_app_config_dir_windows_uses_appdata(monkeypatch):
    # pathlib.Path is always PosixPath on this (Linux) test runner regardless
    # of the platform.system() mock, so it joins with "/" here — on real
    # Windows the same code produces a WindowsPath joined with "\". Assert on
    # parts, not the OS-specific string form: this test can only prove the
    # APPDATA value was used as the base and "GNS3" appended, not the real
    # Windows rendering.
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", r"C:\Users\user\AppData\Roaming")
    result = gns3conf.app_config_dir("GNS3")
    assert result.parts[-1] == "GNS3"
    assert str(result.parent) == r"C:\Users\user\AppData\Roaming"


def test_app_config_dir_windows_missing_appdata_raises(monkeypatch):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Windows")
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(RuntimeError):
        gns3conf.app_config_dir("GNS3")


@pytest.mark.parametrize("system_name", ["Linux", "Darwin"])
def test_app_config_dir_posix_uses_dot_config(monkeypatch, tmp_path, system_name):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: system_name)
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    result = gns3conf.app_config_dir("GNS3")
    assert result == tmp_path / ".config" / "GNS3"


def test_app_config_dir_posix_honours_xdg_config_home(monkeypatch, tmp_path):
    # gns3-gui's own configDirectory() checks $XDG_CONFIG_HOME before
    # falling back to ~/.config — mirrored here so patched files land where
    # the GUI actually reads from.
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    xdg_dir = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
    result = gns3conf.app_config_dir("GNS3")
    assert result == xdg_dir / "GNS3"


def test_gns3_gui_config_path_is_versioned(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.gns3_gui_config_path() == tmp_path / ".config" / "GNS3" / "2.2" / "gns3_gui.conf"


def test_gns3_gui_config_path_uses_ini_extension_on_windows(monkeypatch):
    # gns3-gui's LocalConfig names this file gns3_gui.ini on Windows, not
    # gns3_gui.conf — the GUI silently ignores a file with the wrong name
    # (gns3conf module docstring; verified against local_config.py's
    # _resetLoadConfig()).
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", r"C:\Users\user\AppData\Roaming")
    assert gns3conf.gns3_gui_config_path().name == "gns3_gui.ini"


def test_wrapper_state_path_is_separate_from_gns3(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.wrapper_state_path() == tmp_path / ".config" / "gns3-cloud-lab" / "state.json"


def test_gns3_gui_pid_path_sits_next_to_gui_config(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.gns3_gui_pid_path().parent == gns3conf.gns3_gui_config_path().parent


def test_wrapper_gui_conf_backup_path_lives_in_wrapper_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.wrapper_gui_conf_backup_path().parent == gns3conf.wrapper_state_dir()


def test_gns3_local_server_conf_path_sits_next_to_gui_config(monkeypatch, tmp_path):
    # Same directory as gns3_gui.conf, but a different file — not to be
    # confused with the *remote* gns3_server.conf cli.py writes over SSH
    # onto the VM, which shares the filename but lives on a different
    # machine in a different format (gns3conf module docstring).
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.gns3_local_server_conf_path().parent == gns3conf.gns3_gui_config_path().parent
    assert gns3conf.gns3_local_server_conf_path().name == "gns3_server.conf"


def test_gns3_local_server_conf_path_uses_ini_extension_on_windows(monkeypatch):
    # Same Windows-only rename as gns3_gui.conf/.ini, but for
    # LocalServerConfig — verified against local_server_config.py.
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", r"C:\Users\user\AppData\Roaming")
    assert gns3conf.gns3_local_server_conf_path().name == "gns3_server.ini"


def test_wrapper_local_server_conf_backup_path_lives_in_wrapper_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.wrapper_local_server_conf_backup_path().parent == gns3conf.wrapper_state_dir()


# ---------------------------------------------------------------------------
# gui_is_running
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


def test_gui_is_running_no_pid_file(tmp_path):
    assert gns3conf.gui_is_running(tmp_path / "gns3_gui.pid") is False


def test_gui_is_running_pid_file_has_garbage(tmp_path):
    pid_path = tmp_path / "gns3_gui.pid"
    pid_path.write_text("not-a-pid")
    assert gns3conf.gui_is_running(pid_path) is False


def test_gui_is_running_pid_no_longer_exists(monkeypatch, tmp_path):
    pid_path = tmp_path / "gns3_gui.pid"
    pid_path.write_text("99999")

    def raise_no_such_process(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(gns3conf.psutil, "Process", raise_no_such_process)
    assert gns3conf.gui_is_running(pid_path) is False


def test_gui_is_running_pid_reused_by_unrelated_process(monkeypatch, tmp_path):
    # A dead GNS3 process's PID can, in principle, later be reused by the OS
    # for something unrelated — name-checking guards against that.
    pid_path = tmp_path / "gns3_gui.pid"
    pid_path.write_text("4242")
    monkeypatch.setattr(gns3conf.psutil, "Process", lambda pid: _FakeProcess("firefox"))
    assert gns3conf.gui_is_running(pid_path) is False


def test_gui_is_running_true_for_live_gns3_process(monkeypatch, tmp_path):
    pid_path = tmp_path / "gns3_gui.pid"
    pid_path.write_text("4242")
    monkeypatch.setattr(gns3conf.psutil, "Process", lambda pid: _FakeProcess("gns3_gui"))
    assert gns3conf.gui_is_running(pid_path) is True


def test_gui_is_running_true_for_live_python_process(monkeypatch, tmp_path):
    # GNS3 run from source (not a packaged build) shows up as a "python"
    # process rather than "gns3" — isMainGui() in gns3-gui accepts both.
    pid_path = tmp_path / "gns3_gui.pid"
    pid_path.write_text("4242")
    monkeypatch.setattr(gns3conf.psutil, "Process", lambda pid: _FakeProcess("python3.12"))
    assert gns3conf.gui_is_running(pid_path) is True


# ---------------------------------------------------------------------------
# load_json / save_json
# ---------------------------------------------------------------------------


def test_load_json_missing_file_returns_empty_dict(tmp_path):
    assert gns3conf.load_json(tmp_path / "nope.json") == {}


def test_load_json_empty_file_returns_empty_dict(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("")
    assert gns3conf.load_json(path) == {}


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "sub" / "conf.json"
    data = {"Servers": {"remote_servers": [{"host": "1.2.3.4"}]}}
    gns3conf.save_json(path, data)
    assert gns3conf.load_json(path) == data


def test_save_json_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "c" / "conf.json"
    gns3conf.save_json(path, {})
    assert path.exists()


# ---------------------------------------------------------------------------
# upsert_remote_server
# ---------------------------------------------------------------------------


def test_upsert_into_empty_conf_creates_structure():
    conf = gns3conf.upsert_remote_server(
        {}, host="34.1.2.3", port=3080, protocol="http", user="admin", password="secret"
    )
    assert conf["Servers"]["remote_servers"] == [
        {"host": "34.1.2.3", "port": 3080, "protocol": "http", "user": "admin", "password": "secret"}
    ]


def test_upsert_preserves_unrelated_keys():
    conf = {"MainWindow": {"geometry": "abc"}, "Servers": {"local_server": {"foo": "bar"}}}
    result = gns3conf.upsert_remote_server(
        conf, host="1.1.1.1", port=3080, protocol="http", user="admin", password="pw"
    )
    assert result["MainWindow"] == {"geometry": "abc"}
    assert result["Servers"]["local_server"] == {"foo": "bar"}
    assert len(result["Servers"]["remote_servers"]) == 1


def test_upsert_updates_existing_entry_for_same_user_in_place():
    conf = {
        "Servers": {
            "remote_servers": [
                {"host": "1.1.1.1", "port": 3080, "protocol": "http", "user": "admin", "password": "old"}
            ]
        }
    }
    result = gns3conf.upsert_remote_server(
        conf, host="2.2.2.2", port=3080, protocol="http", user="admin", password="new"
    )
    servers = result["Servers"]["remote_servers"]
    assert len(servers) == 1
    assert servers[0]["host"] == "2.2.2.2"
    assert servers[0]["password"] == "new"


def test_upsert_appends_for_a_different_user_without_touching_others():
    conf = {
        "Servers": {
            "remote_servers": [
                {"host": "1.1.1.1", "port": 3080, "protocol": "http", "user": "other", "password": "x"}
            ]
        }
    }
    result = gns3conf.upsert_remote_server(
        conf, host="2.2.2.2", port=3080, protocol="http", user="admin", password="y"
    )
    servers = result["Servers"]["remote_servers"]
    assert len(servers) == 2
    assert {s["user"] for s in servers} == {"other", "admin"}


def test_upsert_does_not_mutate_input_conf():
    conf = {"Servers": {"remote_servers": []}}
    gns3conf.upsert_remote_server(
        conf, host="1.1.1.1", port=3080, protocol="http", user="admin", password="pw"
    )
    assert conf["Servers"]["remote_servers"] == []


# ---------------------------------------------------------------------------
# patch_gui_conf (glue)
# ---------------------------------------------------------------------------


def test_patch_gui_conf_writes_file_with_new_ip(tmp_path):
    path = tmp_path / "gns3_gui.conf"
    result = gns3conf.patch_gui_conf(
        path, host="34.1.2.3", port=3080, protocol="http", user="admin", password="pw1"
    )
    on_disk = json.loads(path.read_text())
    assert on_disk == result
    assert on_disk["Servers"]["remote_servers"][0]["host"] == "34.1.2.3"


def test_patch_gui_conf_second_call_refreshes_ip_and_password(tmp_path):
    path = tmp_path / "gns3_gui.conf"
    gns3conf.patch_gui_conf(path, host="34.1.2.3", port=3080, protocol="http", user="admin", password="pw1")
    result = gns3conf.patch_gui_conf(
        path, host="35.9.9.9", port=3080, protocol="http", user="admin", password="pw2"
    )
    servers = result["Servers"]["remote_servers"]
    assert len(servers) == 1
    assert servers[0]["host"] == "35.9.9.9"
    assert servers[0]["password"] == "pw2"


# ---------------------------------------------------------------------------
# patch_local_server_conf
# ---------------------------------------------------------------------------


def _read_server_section(path):
    config = configparser.RawConfigParser()
    config.read(path, encoding="utf-8")
    return dict(config["Server"])


def test_patch_local_server_conf_writes_ini_with_server_section(tmp_path):
    path = tmp_path / "gns3_server.conf"
    gns3conf.patch_local_server_conf(
        path, host="34.1.2.3", port=3080, protocol="http", user="admin", password="pw1"
    )
    section = _read_server_section(path)
    assert section["host"] == "34.1.2.3"
    assert section["port"] == "3080"
    assert section["protocol"] == "http"
    assert section["user"] == "admin"
    assert section["password"] == "pw1"


def test_patch_local_server_conf_auth_and_auto_start(tmp_path):
    # auto_start=False is what makes GNS3 GUI treat this as its main server
    # instead of trying to run/use a local one (gns3conf module docstring) —
    # confirmed from gns3-gui's own source, not yet from a live re-test.
    path = tmp_path / "gns3_server.conf"
    gns3conf.patch_local_server_conf(
        path, host="34.1.2.3", port=3080, protocol="http", user="admin", password="pw1"
    )
    section = _read_server_section(path)
    assert section["auth"] == "True"
    assert section["auto_start"] == "False"


def test_patch_local_server_conf_preserves_unrelated_keys(tmp_path):
    path = tmp_path / "gns3_server.conf"
    path.write_text("[Server]\nimages_path = /home/user/GNS3/images\n\n[Other]\nfoo = bar\n")
    gns3conf.patch_local_server_conf(
        path, host="34.1.2.3", port=3080, protocol="http", user="admin", password="pw1"
    )
    config = configparser.RawConfigParser()
    config.read(path, encoding="utf-8")
    assert config["Server"]["images_path"] == "/home/user/GNS3/images"
    assert config["Other"]["foo"] == "bar"


def test_patch_local_server_conf_second_call_refreshes_ip_and_password(tmp_path):
    path = tmp_path / "gns3_server.conf"
    gns3conf.patch_local_server_conf(
        path, host="34.1.2.3", port=3080, protocol="http", user="admin", password="pw1"
    )
    gns3conf.patch_local_server_conf(
        path, host="35.9.9.9", port=3080, protocol="http", user="admin", password="pw2"
    )
    section = _read_server_section(path)
    assert section["host"] == "35.9.9.9"
    assert section["password"] == "pw2"
