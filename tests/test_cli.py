import io
import json

import pytest

from gns3_2620_lab import cli, gcp, gns3conf


def make_ctx(**overrides):
    defaults = dict(
        gcloud_exe="gcloud",
        project="proj",
        zone="us-west1-b",
        instance="gns3-lab",
        config_name=None,
    )
    defaults.update(overrides)
    return gcp.GcpContext(**defaults)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Point wrapper state and GNS3 GUI config at a throwaway directory so no
    test touches the real filesystem outside tmp_path."""
    monkeypatch.setattr(gns3conf, "wrapper_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(gns3conf, "gns3_gui_config_path", lambda: tmp_path / "gns3_gui.conf")
    return tmp_path


# ---------------------------------------------------------------------------
# module constants
# ---------------------------------------------------------------------------


def test_firewall_ports_matches_console_range_constants():
    # Regression guard: FIREWALL_PORTS must stay derived from
    # CONSOLE_PORT_START/END, not a second hardcoded copy of the range that
    # can silently drift out of sync with it.
    assert cli.FIREWALL_PORTS == [
        f"tcp:{cli.SERVER_PORT}",
        f"tcp:{cli.CONSOLE_PORT_START}-{cli.CONSOLE_PORT_END}",
    ]


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def test_parser_defaults():
    args = cli.build_parser().parse_args(["status"])
    assert args.instance == cli.DEFAULT_INSTANCE
    assert args.zone == cli.DEFAULT_ZONE
    assert args.project is None
    assert args.gcloud_config is None
    assert args.command == "status"


def test_parser_overrides():
    args = cli.build_parser().parse_args(
        ["--project", "p1", "--instance", "scratch", "--zone", "us-east1-b", "--gcloud-config", "cfg", "create"]
    )
    assert (args.project, args.instance, args.zone, args.gcloud_config) == ("p1", "scratch", "us-east1-b", "cfg")


def test_parser_requires_a_command():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


# ---------------------------------------------------------------------------
# cmd_create
# ---------------------------------------------------------------------------


def test_create_when_instance_already_exists_is_a_noop(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    called = []
    monkeypatch.setattr(gcp, "create_instance", lambda *a, **k: called.append("create_instance"))
    out = io.StringIO()
    assert cli.cmd_create(ctx, out=out) == 0
    assert "already exists" in out.getvalue()
    assert called == []


def test_create_new_instance_creates_firewall_and_instance_and_saves_state(monkeypatch, tmp_path):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)

    created_fw = {}
    monkeypatch.setattr(
        gcp,
        "create_firewall_rule",
        lambda c, **kw: created_fw.update(kw),
    )
    updated_fw = []
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: updated_fw.append(kw))

    created_instance = {}
    monkeypatch.setattr(
        gcp, "create_instance", lambda c, **kw: created_instance.update(kw) or {"id": "1"}
    )

    out = io.StringIO()
    assert cli.cmd_create(ctx, out=out) == 0

    assert created_fw["name"] == "gns3-lab-gns3"
    assert created_fw["source_range"] == "9.9.9.9/32"
    assert updated_fw == []  # rule didn't exist, so create not update
    assert created_instance["tags"] == [cli.DEFAULT_TAG]

    state = json.loads((tmp_path / "state.json").read_text())
    entry = state["proj/us-west1-b/gns3-lab"]
    assert entry["user"] == cli.GUI_USER
    assert entry["firewall_rule"] == "gns3-lab-gns3"
    assert len(entry["password"]) > 10


def test_create_reuses_existing_firewall_rule(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    created = []
    updated = []
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: created.append(kw))
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: updated.append(kw))
    monkeypatch.setattr(gcp, "create_instance", lambda c, **kw: {"id": "1"})

    cli.cmd_create(ctx, out=io.StringIO())

    assert created == []
    assert updated[0]["source_range"] == "9.9.9.9/32"


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------


def _seed_state(tmp_path, ctx, **entry_overrides):
    entry = {
        "project": ctx.project,
        "zone": ctx.zone,
        "instance": ctx.instance,
        "user": "admin",
        "password": "pw123456789",
        "firewall_rule": "gns3-lab-gns3",
    }
    entry.update(entry_overrides)
    state = {cli._state_key(ctx): entry}
    (tmp_path / "state.json").write_text(json.dumps(state))
    return entry


def test_start_without_prior_create_fails(monkeypatch):
    ctx = make_ctx()
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "--create first" in out.getvalue()


def test_start_state_present_but_instance_gone_in_gcp(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: None)
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "out of sync" in out.getvalue()


def test_start_happy_path_starts_refreshes_firewall_and_patches_gui(monkeypatch, tmp_path):
    ctx = make_ctx()
    entry = _seed_state(tmp_path, ctx)

    monkeypatch.setattr(gcp, "instance_status", lambda c: "TERMINATED")
    started = []
    monkeypatch.setattr(gcp, "start_instance", lambda c: started.append(True))
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    fw_updates = []
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: fw_updates.append(kw))

    ssh_calls = []
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: ssh_calls.append(command))
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    out = io.StringIO()
    rc = cli.cmd_start(ctx, out=out)

    assert rc == 0
    assert started == [True]
    assert fw_updates[0]["source_range"] == "9.9.9.9/32"
    assert len(ssh_calls) == 2  # two separate --command calls, CLAUDE.md §2.9
    assert "usermod" in ssh_calls[0]
    assert "gns3server" in ssh_calls[1]

    gui_conf = json.loads((tmp_path / "gns3_gui.conf").read_text())
    remote = gui_conf["Servers"]["remote_servers"][0]
    assert remote["host"] == "34.1.2.3"
    assert remote["password"] == entry["password"]

    state = json.loads((tmp_path / "state.json").read_text())
    assert state[cli._state_key(ctx)]["external_ip"] == "34.1.2.3"

    output = out.getvalue()
    assert "34.1.2.3" in output
    assert "ready" in output


def test_start_already_running_does_not_call_start_instance(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    called = []
    monkeypatch.setattr(gcp, "start_instance", lambda c: called.append(True))
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    assert cli.cmd_start(ctx, out=io.StringIO()) == 0
    assert called == []


def test_start_no_external_ip_fails_cleanly(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: None)
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "never got an external IP" in out.getvalue()


def test_start_server_not_ready_returns_nonzero_but_still_writes_config(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: False)

    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "NOT responding" in out.getvalue()
    assert (tmp_path / "gns3_gui.conf").exists()


# ---------------------------------------------------------------------------
# cmd_stop
# ---------------------------------------------------------------------------


def test_stop_missing_instance(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_status", lambda c: None)
    assert cli.cmd_stop(ctx, out=io.StringIO()) == 1


def test_stop_already_terminated_is_a_noop(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_status", lambda c: "TERMINATED")
    called = []
    monkeypatch.setattr(gcp, "stop_instance", lambda c: called.append(True))
    assert cli.cmd_stop(ctx, out=io.StringIO()) == 0
    assert called == []


def test_stop_running_instance_stops_and_waits(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    called = []
    monkeypatch.setattr(gcp, "stop_instance", lambda c: called.append(True))
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: "TERMINATED")
    out = io.StringIO()
    assert cli.cmd_stop(ctx, out=out) == 0
    assert called == [True]
    assert "TERMINATED" in out.getvalue()


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


def test_status_missing_instance(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_status", lambda c: None)
    assert cli.cmd_status(ctx, out=io.StringIO()) == 1


def test_status_running_with_known_state_prints_credentials(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx, password="topsecret")
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "instance_external_ip", lambda c: "34.1.2.3")
    out = io.StringIO()
    assert cli.cmd_status(ctx, out=out) == 0
    text = out.getvalue()
    assert "RUNNING" in text
    assert "34.1.2.3" in text
    assert "topsecret" in text


def test_status_stopped_does_not_query_external_ip(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "TERMINATED")

    def fail_if_called(c):
        raise AssertionError("should not query external IP for a stopped VM")

    monkeypatch.setattr(gcp, "instance_external_ip", fail_if_called)
    out = io.StringIO()
    assert cli.cmd_status(ctx, out=out) == 0
    assert "(none)" in out.getvalue()


def test_status_unknown_to_wrapper_instance_notes_no_local_record(monkeypatch, tmp_path):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "instance_external_ip", lambda c: "1.2.3.4")
    out = io.StringIO()
    assert cli.cmd_status(ctx, out=out) == 0
    assert "No local record" in out.getvalue()


# ---------------------------------------------------------------------------
# main() wiring / error handling
# ---------------------------------------------------------------------------


def test_main_reports_gcp_error_and_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(gcp, "find_gcloud", lambda: (_ for _ in ()).throw(gcp.GcpError("no gcloud")))
    rc = cli.main(["status"])
    assert rc == 1
    assert "no gcloud" in capsys.readouterr().err


def test_main_dispatches_to_status(monkeypatch):
    monkeypatch.setattr(gcp, "find_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "active-proj")
    called = {}

    def fake_status(ctx, out=None):
        called["ctx"] = ctx
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    monkeypatch.setitem(cli._COMMANDS, "status", fake_status)
    assert cli.main(["status"]) == 0
    assert called["ctx"].project == "active-proj"


# ---------------------------------------------------------------------------
# _remote_setup_command
# ---------------------------------------------------------------------------


def test_remote_setup_command_embeds_valid_json_config():
    command = cli._remote_setup_command(user="admin", password="p@ss")
    opener = "<<'GNS3_SERVER_CONF_EOF'\n"
    start = command.index(opener) + len(opener)
    end = command.rindex("\nGNS3_SERVER_CONF_EOF")
    payload = json.loads(command[start:end])
    assert payload["Server"]["host"] == "0.0.0.0"
    assert payload["Server"]["auth"] is True
    assert payload["Server"]["user"] == "admin"
    assert payload["Server"]["password"] == "p@ss"
    assert payload["Server"]["console_start_port_range"] == cli.CONSOLE_PORT_START
    assert payload["Server"]["console_end_port_range"] == cli.CONSOLE_PORT_END


def test_remote_setup_command_uses_single_quoted_heredoc_delimiter():
    command = cli._remote_setup_command(user="admin", password="pw")
    assert "<<'GNS3_SERVER_CONF_EOF'" in command


# ---------------------------------------------------------------------------
# _remote_launch_command
# ---------------------------------------------------------------------------


def test_remote_launch_command_does_not_use_pgrep():
    # Regression test: `pgrep -f gns3server` matches the invoking shell's
    # own command line (which necessarily contains the literal text
    # "gns3server", the pattern itself) and only excludes pgrep's own PID,
    # not its parent shell — so it always self-matches and the launch line
    # never runs. Confirmed on a live VM: gns3server.log never got created.
    command = cli._remote_launch_command()
    assert "pgrep" not in command


def test_remote_launch_command_uses_a_pid_file():
    command = cli._remote_launch_command()
    assert "gns3server.pid" in command
    assert "kill -0" in command
    assert "setsid nohup gns3server" in command
    assert "echo $!" in command
