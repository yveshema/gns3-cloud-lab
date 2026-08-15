import json

import pytest

from gns3_2620_lab import gns3conf


# ---------------------------------------------------------------------------
# app_config_dir / path helpers
# ---------------------------------------------------------------------------


def test_app_config_dir_windows_uses_appdata(monkeypatch):
    # pathlib.Path is always PosixPath on this (Linux) test runner regardless
    # of the platform.system() mock, so it joins with "/" here — on real
    # Windows the same code produces a WindowsPath joined with "\". Assert on
    # parts, not the OS-specific string form, per CLAUDE.md's Windows/macOS
    # paths being unverified: this test can only prove the APPDATA value was
    # used as the base and "GNS3" appended, not the real Windows rendering.
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", r"C:\Users\student\AppData\Roaming")
    result = gns3conf.app_config_dir("GNS3")
    assert result.parts[-1] == "GNS3"
    assert str(result.parent) == r"C:\Users\student\AppData\Roaming"


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


def test_gns3_gui_config_path_is_versioned(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.gns3_gui_config_path() == tmp_path / ".config" / "GNS3" / "2.2" / "gns3_gui.conf"


def test_wrapper_state_path_is_separate_from_gns3(monkeypatch, tmp_path):
    monkeypatch.setattr(gns3conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gns3conf.Path, "home", lambda: tmp_path)
    assert gns3conf.wrapper_state_path() == tmp_path / ".config" / "gns3-2620-lab" / "state.json"


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
