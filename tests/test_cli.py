import configparser
import io
import json
import shutil
import subprocess

import pytest

from gns3_cloud_lab import cli, gcp, gns3conf


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
    test touches the real filesystem outside tmp_path — including whatever
    gns3_gui.pid a real GNS3 install may have left on the machine running
    the tests."""
    monkeypatch.setattr(gns3conf, "wrapper_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(gns3conf, "gns3_gui_config_path", lambda: tmp_path / "gns3_gui.conf")
    monkeypatch.setattr(gns3conf, "gns3_gui_pid_path", lambda: tmp_path / "gns3_gui.pid")
    monkeypatch.setattr(gns3conf, "wrapper_gui_conf_backup_path", lambda: tmp_path / "gns3_gui.conf.bak")
    monkeypatch.setattr(gns3conf, "gns3_local_server_conf_path", lambda: tmp_path / "gns3_server.conf")
    monkeypatch.setattr(
        gns3conf, "wrapper_local_server_conf_backup_path", lambda: tmp_path / "gns3_server.conf.bak"
    )
    monkeypatch.setattr(gns3conf, "gui_is_running", lambda *a, **k: False)
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
    assert args.zone is None  # resolved later from active gcloud config, not a hardcoded literal
    assert args.project is None
    assert args.gcloud_config is None
    assert args.command == "status"


def test_parser_enroll_ssh_user_defaults_to_none():
    args = cli.build_parser().parse_args(["enroll"])
    assert args.ssh_user is None


def test_parser_enroll_accepts_ssh_user():
    args = cli.build_parser().parse_args(["enroll", "--ssh-user", "rys"])
    assert args.ssh_user == "rys"


def test_parser_overrides():
    args = cli.build_parser().parse_args(
        ["--project", "p1", "--instance", "scratch", "--zone", "us-east1-b", "--gcloud-config", "cfg", "create"]
    )
    assert (args.project, args.instance, args.zone, args.gcloud_config) == ("p1", "scratch", "us-east1-b", "cfg")


def test_refresh_is_an_alias_of_start_in_dispatch_table():
    # aliases=["refresh"] makes argparse accept "refresh" as a command name,
    # but dispatch is a separate dict lookup (_COMMANDS) that must be kept
    # in sync by hand — this guards against that drifting.
    assert cli._COMMANDS["refresh"] is cli._COMMANDS["start"]


def test_parser_accepts_refresh_as_command():
    args = cli.build_parser().parse_args(["refresh"])
    assert args.command == "refresh"


def test_parser_with_no_command_leaves_command_none():
    # main() turns None into "scan" — the parser itself just records the
    # absence, so it stays testable independent of that default.
    args = cli.build_parser().parse_args([])
    assert args.command is None


def test_main_with_no_command_dispatches_to_scan(monkeypatch):
    monkeypatch.setattr(gcp, "find_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "active-proj")
    called = {}

    def fake_scan(ctx, **kw):
        called["ctx"] = ctx
        return 0

    monkeypatch.setitem(cli._COMMANDS, "scan", fake_scan)
    assert cli.main([]) == 0
    assert called["ctx"].project == "active-proj"


# ---------------------------------------------------------------------------
# cmd_scan
# ---------------------------------------------------------------------------


def test_scan_no_instances(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "list_instances", lambda exe, project, **k: [])
    out = io.StringIO()
    assert cli.cmd_scan(ctx, out=out) == 0
    assert "No instances found" in out.getvalue()


def test_scan_lists_instances_and_flags_which_are_tracked(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)  # tracks "gns3-lab"
    monkeypatch.setattr(
        gcp,
        "list_instances",
        lambda exe, project, **k: [
            {"name": "gns3-lab", "zone": ".../zones/us-west1-b", "status": "RUNNING"},
            {"name": "instructor-vm", "zone": ".../zones/us-central1-a", "status": "TERMINATED"},
        ],
    )
    out = io.StringIO()
    assert cli.cmd_scan(ctx, out=out) == 0
    text = out.getvalue()
    assert "gns3-lab  zone=us-west1-b  status=RUNNING  (tracked by this tool)" in text
    assert "instructor-vm  zone=us-central1-a  status=TERMINATED  (not tracked" in text


def test_main_scan_passes_project_only_no_instance_required(monkeypatch):
    # scan is meant to work before the user knows an instance name at all —
    # it must not depend on --instance being set to anything meaningful.
    monkeypatch.setattr(gcp, "find_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "active-proj")
    monkeypatch.setattr(gcp, "list_instances", lambda exe, project, **k: [])
    assert cli.main(["scan"]) == 0


# ---------------------------------------------------------------------------
# cmd_create
# ---------------------------------------------------------------------------


def test_create_dry_run_does_not_create_anything(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    called = []
    monkeypatch.setattr(gcp, "create_instance", lambda *a, **k: called.append("create_instance"))
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda *a, **k: called.append("create_firewall_rule"))

    out = io.StringIO()
    assert cli.cmd_create(ctx, out=out, dry_run=True) == 0
    assert called == []
    assert "Would create instance" in out.getvalue()


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


def test_create_announces_each_step_before_running_it(monkeypatch):
    # Regression: create used to run its whole prelude (describe, public IP
    # lookup, firewall rule) and then instance creation with nothing printed
    # at all, so a slow run was indistinguishable from a hang. Each step
    # must announce itself *before* the call it covers, so the assertions
    # below record call order interleaved with output, not just the text.
    ctx = make_ctx()
    events = []

    def record(name, result):
        events.append(name)
        return result

    monkeypatch.setattr(gcp, "instance_describe", lambda c: record("describe", None))
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: record("public_ip", "9.9.9.9"))
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: record("firewall", None))
    monkeypatch.setattr(gcp, "create_instance", lambda c, **kw: record("create_instance", None))

    class Recorder(io.StringIO):
        def write(self, text):
            if text.strip():
                events.append(f"out:{text.strip()}")
            return super().write(text)

    out = Recorder()
    assert cli.cmd_create(ctx, out=out) == 0

    # Every gcloud/HTTP call is immediately preceded by a line of output.
    for call in ("describe", "public_ip", "firewall", "create_instance"):
        assert events[events.index(call) - 1].startswith("out:"), f"{call} ran with no announcement"


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
# _validate_ssh_user
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "rys@gns3-lab", "CORP\\rys", "yves rene", "a:b", "a/b"])
def test_validate_ssh_user_rejects_unsafe_values(bad):
    assert cli._validate_ssh_user(bad) is not None


@pytest.mark.parametrize("good", ["rys", "yvess", "instructor-01"])
def test_validate_ssh_user_accepts_plain_names(good):
    assert cli._validate_ssh_user(good) is None


# ---------------------------------------------------------------------------
# cmd_enroll
# ---------------------------------------------------------------------------


def test_enroll_rejects_invalid_ssh_user_without_touching_gcp(monkeypatch, tmp_path):
    ctx = make_ctx()
    called = []
    monkeypatch.setattr(gcp, "instance_describe", lambda c: called.append(True))
    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out, ssh_user="rys@gns3-lab") == 1
    assert "doesn't look like a Linux username" in out.getvalue()
    assert called == []
    assert not (tmp_path / "state.json").exists()


def test_enroll_applies_ssh_user_to_its_own_ssh_calls(monkeypatch, tmp_path):
    # Regression guard: an ssh_user only saved for a later start to pick up
    # (instead of applied to ctx immediately) would leave enroll's own
    # credential discovery running against gcloud's default account.
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    seen_ssh_user_at_ready_check = []
    monkeypatch.setattr(
        gcp,
        "wait_for_ssh_ready",
        lambda c, **k: seen_ssh_user_at_ready_check.append(c.ssh_user) or True,
    )
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)
    seen_ssh_user_at_discovery = []
    monkeypatch.setattr(
        gcp,
        "ssh_run",
        lambda c, command, **k: seen_ssh_user_at_discovery.append(c.ssh_user)
        or _fake_completed(returncode=1, stdout=""),
    )

    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out, ssh_user="rys") == 0
    assert seen_ssh_user_at_ready_check == ["rys"]
    assert seen_ssh_user_at_discovery == ["rys"]


def test_enroll_saves_ssh_user_in_state(monkeypatch, tmp_path):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: _fake_completed(returncode=1, stdout=""))

    cli.cmd_enroll(ctx, out=io.StringIO(), ssh_user="rys")

    state = json.loads((tmp_path / "state.json").read_text())
    assert state[cli._state_key(ctx)]["ssh_user"] == "rys"


def test_enroll_without_ssh_user_saves_none(monkeypatch, tmp_path):
    # start reads this back with entry.get("ssh_user") — must be present (as
    # None) so an already-enrolled VM predating this feature and a freshly
    # enrolled one without --ssh-user behave identically.
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: _fake_completed(returncode=1, stdout=""))

    cli.cmd_enroll(ctx, out=io.StringIO())

    state = json.loads((tmp_path / "state.json").read_text())
    assert state[cli._state_key(ctx)]["ssh_user"] is None


def test_enroll_already_enrolled_is_a_noop(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    called = []
    monkeypatch.setattr(gcp, "instance_describe", lambda c: called.append(True))
    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 0
    assert "already enrolled" in out.getvalue()
    assert called == []


def test_enroll_instance_missing_in_gcp_fails(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 1
    assert "does not exist" in out.getvalue()


def test_enroll_reuses_credentials_found_on_the_instance(monkeypatch, tmp_path):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)

    remote_conf = "[Server]\nuser = instructor\npassword = already-set\n"
    monkeypatch.setattr(
        gcp,
        "ssh_run",
        lambda c, command, **k: _fake_completed(returncode=0, stdout=remote_conf),
    )

    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 0

    state = json.loads((tmp_path / "state.json").read_text())
    entry = state[cli._state_key(ctx)]
    assert entry["user"] == "instructor"
    assert entry["password"] == "already-set"


def test_enroll_generates_password_when_nothing_configured_yet(monkeypatch, tmp_path):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)
    monkeypatch.setattr(
        gcp, "ssh_run", lambda c, command, **k: _fake_completed(returncode=1, stdout="")
    )

    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 0

    state = json.loads((tmp_path / "state.json").read_text())
    entry = state[cli._state_key(ctx)]
    assert entry["user"] == cli.GUI_USER
    assert len(entry["password"]) > 10


def test_enroll_starts_a_stopped_vm_before_discovering_credentials(monkeypatch, tmp_path):
    # The point of the feature: enroll must not fall straight to generating
    # a password just because the VM was off — it starts it first, same as
    # start would, so credential discovery gets a real answer.
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "TERMINATED"})
    started = []
    monkeypatch.setattr(gcp, "start_instance", lambda c: started.append(True))
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: target)
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)
    remote_conf = "[Server]\nuser = instructor\npassword = already-set\n"
    monkeypatch.setattr(
        gcp, "ssh_run", lambda c, command, **k: _fake_completed(returncode=0, stdout=remote_conf)
    )

    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 0
    assert started == [True]

    state = json.loads((tmp_path / "state.json").read_text())
    entry = state[cli._state_key(ctx)]
    assert entry["user"] == "instructor"
    assert entry["password"] == "already-set"


def test_enroll_refuses_when_ssh_never_becomes_ready(monkeypatch, tmp_path):
    # Regression test: falling through to a freshly generated password here
    # would mean the next start silently overwrites real credentials on a
    # VM this tool never actually confirmed it could reach.
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: False)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: False)
    monkeypatch.setattr(gcp, "create_firewall_rule", lambda c, **kw: None)
    ssh_run_calls = []
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: ssh_run_calls.append(command))

    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 1
    assert "never accepted an SSH connection" in out.getvalue()
    assert ssh_run_calls == []  # never got to credential discovery
    assert not (tmp_path / "state.json").exists()


def test_enroll_refuses_when_no_external_ip(monkeypatch, tmp_path):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: None)

    out = io.StringIO()
    assert cli.cmd_enroll(ctx, out=out) == 1
    assert "never got an external IP" in out.getvalue()
    assert not (tmp_path / "state.json").exists()


def _fake_completed(*, returncode, stdout):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------


def test_start_refuses_when_gui_is_running(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gns3conf, "gui_is_running", lambda *a, **k: True)
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "close it first" in out.getvalue().lower()


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
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "--create first" in out.getvalue()


def test_start_without_local_record_but_exists_in_gcp_suggests_enroll(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "--enroll" in out.getvalue()


def test_start_state_present_but_instance_gone_in_gcp(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: None)
    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "out of sync" in out.getvalue()


def test_progress_prints_label_dots_then_newline():
    out = io.StringIO()
    with cli._progress(out, "Waiting for X") as tick:
        tick()
        tick()
    assert out.getvalue() == "Waiting for X.....\n"


def test_start_shows_progress_dots_while_waiting_for_running(monkeypatch, tmp_path):
    # A silent terminal for over a minute is indistinguishable from a hang
    # — this checks the wait loops actually get wired to something visible,
    # not just that on_tick is accepted as a parameter.
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "TERMINATED")
    monkeypatch.setattr(gcp, "start_instance", lambda c: None)

    def fake_wait_for_status(c, target, **k):
        k["on_tick"]()
        k["on_tick"]()
        return "RUNNING"

    monkeypatch.setattr(gcp, "wait_for_status", fake_wait_for_status)
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    out = io.StringIO()
    cli.cmd_start(ctx, out=out)
    # label's own "..." plus one "." per on_tick() call (two, above)
    assert "Waiting for instance to report RUNNING.....\n" in out.getvalue()


def test_start_happy_path_starts_refreshes_firewall_and_patches_gui(monkeypatch, tmp_path):
    ctx = make_ctx()
    entry = _seed_state(tmp_path, ctx)

    monkeypatch.setattr(gcp, "instance_status", lambda c: "TERMINATED")
    started = []
    monkeypatch.setattr(gcp, "start_instance", lambda c: started.append(True))
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
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
    assert len(ssh_calls) == 2  # two separate --command calls: group membership needs a fresh login
    assert "usermod" in ssh_calls[0]
    assert "gns3server" in ssh_calls[1]

    gui_conf = json.loads((tmp_path / "gns3_gui.conf").read_text())
    remote = gui_conf["Servers"]["remote_servers"][0]
    assert remote["host"] == "34.1.2.3"
    assert remote["password"] == entry["password"]

    # This is the file Preferences -> Server -> "Remote main server host"
    # actually reads (gns3conf module docstring) — remote_servers alone
    # left that field on "localhost" on a real machine.
    local_server_config = configparser.RawConfigParser()
    local_server_config.read(tmp_path / "gns3_server.conf", encoding="utf-8")
    assert local_server_config["Server"]["host"] == "34.1.2.3"
    assert local_server_config["Server"]["password"] == entry["password"]
    assert local_server_config["Server"]["auto_start"] == "False"

    state = json.loads((tmp_path / "state.json").read_text())
    assert state[cli._state_key(ctx)]["external_ip"] == "34.1.2.3"

    output = out.getvalue()
    assert "34.1.2.3" in output
    assert "ready" in output


def test_start_applies_ssh_user_saved_by_enroll(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx, ssh_user="rys")
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    seen = []
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: seen.append(c.ssh_user) or "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: seen.append(c.ssh_user))
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    assert cli.cmd_start(ctx, out=io.StringIO()) == 0
    assert seen == ["rys", "rys", "rys"]


def test_start_without_ssh_user_in_state_leaves_default_behavior(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)  # no ssh_user key at all — predates this feature
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    seen = []
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: seen.append(c.ssh_user) or "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    assert cli.cmd_start(ctx, out=io.StringIO()) == 0
    assert seen == [None]


def test_start_already_running_does_not_call_start_instance(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    called = []
    monkeypatch.setattr(gcp, "start_instance", lambda c: called.append(True))
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
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


def test_start_ssh_never_ready_fails_cleanly_without_pushing_config(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: False)
    ssh_calls = []
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: ssh_calls.append(command))

    out = io.StringIO()
    assert cli.cmd_start(ctx, out=out) == 1
    assert "never accepted an SSH connection" in out.getvalue()
    assert ssh_calls == []  # never got to setup/launch


def test_start_server_not_ready_returns_nonzero_but_still_writes_config(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
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
# GUI config backup / restore
# ---------------------------------------------------------------------------


def test_start_backs_up_existing_gui_conf_and_stop_restores_it(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    (tmp_path / "gns3_gui.conf").write_text('{"MainWindow": {"geometry": "original"}}')

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    assert cli.cmd_start(ctx, out=io.StringIO()) == 0
    # patched (Servers.remote_servers added) but MainWindow preserved, per
    # upsert_remote_server's contract — the backup, not the live file, is
    # what should hold the untouched original.
    live_conf = json.loads((tmp_path / "gns3_gui.conf").read_text())
    assert "remote_servers" in live_conf["Servers"]
    assert (tmp_path / "gns3_gui.conf.bak").read_text() == '{"MainWindow": {"geometry": "original"}}'

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "stop_instance", lambda c: None)
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: "TERMINATED")
    out = io.StringIO()
    assert cli.cmd_stop(ctx, out=out) == 0

    assert (tmp_path / "gns3_gui.conf").read_text() == '{"MainWindow": {"geometry": "original"}}'
    assert not (tmp_path / "gns3_gui.conf.bak").exists()
    assert "Restored" in out.getvalue()


def test_start_backs_up_existing_local_server_conf_and_stop_restores_it(monkeypatch, tmp_path):
    # Same round-trip as gns3_gui.conf, for the second client-side file
    # start/refresh now also patches (gns3conf module docstring) — a
    # personal machine's own real local-server settings (images_path etc.)
    # must survive a start/stop cycle just as much as gns3_gui.conf's.
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    (tmp_path / "gns3_server.conf").write_text(
        "[Server]\nimages_path = /home/user/GNS3/images\n"
    )

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    assert cli.cmd_start(ctx, out=io.StringIO()) == 0
    live_config = configparser.RawConfigParser()
    live_config.read(tmp_path / "gns3_server.conf", encoding="utf-8")
    assert live_config["Server"]["host"] == "34.1.2.3"  # patched
    assert live_config["Server"]["images_path"] == "/home/user/GNS3/images"  # preserved
    assert (tmp_path / "gns3_server.conf.bak").read_text() == "[Server]\nimages_path = /home/user/GNS3/images\n"

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "stop_instance", lambda c: None)
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: "TERMINATED")
    assert cli.cmd_stop(ctx, out=io.StringIO()) == 0

    assert (tmp_path / "gns3_server.conf").read_text() == "[Server]\nimages_path = /home/user/GNS3/images\n"
    assert not (tmp_path / "gns3_server.conf.bak").exists()


def test_start_with_no_prior_gui_conf_and_stop_removes_it(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    assert not (tmp_path / "gns3_gui.conf").exists()

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)
    cli.cmd_start(ctx, out=io.StringIO())
    assert (tmp_path / "gns3_gui.conf").exists()

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "stop_instance", lambda c: None)
    monkeypatch.setattr(gcp, "wait_for_status", lambda c, target, **k: "TERMINATED")
    cli.cmd_stop(ctx, out=io.StringIO())

    assert not (tmp_path / "gns3_gui.conf").exists()


def test_second_start_without_a_stop_does_not_reclobber_the_backup(monkeypatch, tmp_path):
    ctx = make_ctx()
    _seed_state(tmp_path, ctx)
    (tmp_path / "gns3_gui.conf").write_text('{"MainWindow": {"geometry": "original"}}')

    monkeypatch.setattr(gcp, "instance_status", lambda c: "RUNNING")
    monkeypatch.setattr(gcp, "wait_for_external_ip", lambda c, **k: "34.1.2.3")
    monkeypatch.setattr(gcp, "wait_for_ssh_ready", lambda c, **k: True)
    monkeypatch.setattr(gcp, "get_public_ipv4", lambda: "9.9.9.9")
    monkeypatch.setattr(gcp, "firewall_rule_exists", lambda c, name: True)
    monkeypatch.setattr(gcp, "update_firewall_source_range", lambda c, **kw: None)
    monkeypatch.setattr(gcp, "ssh_run", lambda c, command, **k: None)
    monkeypatch.setattr(gcp, "wait_for_http_ready", lambda host, port, **k: True)

    cli.cmd_start(ctx, out=io.StringIO())
    cli.cmd_start(ctx, out=io.StringIO())  # no --stop in between

    assert (tmp_path / "gns3_gui.conf.bak").read_text() == '{"MainWindow": {"geometry": "original"}}'


# ---------------------------------------------------------------------------
# cmd_stop
# ---------------------------------------------------------------------------


def test_stop_refuses_when_gui_is_running(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gns3conf, "gui_is_running", lambda *a, **k: True)
    out = io.StringIO()
    assert cli.cmd_stop(ctx, out=out) == 1
    assert "close it first" in out.getvalue().lower()


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


def test_main_missing_gcloud_shows_setup_instructions(monkeypatch, capsys):
    monkeypatch.setattr(
        gcp, "find_gcloud", lambda: (_ for _ in ()).throw(gcp.GcloudNotFoundError("gcloud missing"))
    )
    rc = cli.main(["status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "gcloud missing" in err
    assert "gcloud config configurations create" in err  # from setup_help.SETUP_INSTRUCTIONS


def test_main_dispatches_to_status(monkeypatch):
    monkeypatch.setattr(gcp, "find_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "active-proj")
    monkeypatch.setattr(gcp, "get_active_zone", lambda exe, cfg: "active-zone")
    called = {}

    def fake_status(ctx, out=None):
        called["ctx"] = ctx
        return 0

    monkeypatch.setattr(cli, "cmd_status", fake_status)
    monkeypatch.setitem(cli._COMMANDS, "status", fake_status)
    assert cli.main(["status"]) == 0
    assert called["ctx"].project == "active-proj"
    assert called["ctx"].zone == "active-zone"


def test_main_enroll_passes_ssh_user_through(monkeypatch):
    monkeypatch.setattr(gcp, "find_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "active-proj")
    monkeypatch.setattr(gcp, "get_active_zone", lambda exe, cfg: "active-zone")
    called = {}

    def fake_enroll(ctx, **kw):
        called.update(kw)
        return 0

    monkeypatch.setitem(cli._COMMANDS, "enroll", fake_enroll)
    assert cli.main(["enroll", "--ssh-user", "rys"]) == 0
    assert called["ssh_user"] == "rys"


def test_main_scan_does_not_require_an_active_zone(monkeypatch):
    # The regression this guards: scan is meant to work before a user
    # knows their zone at all, so it must not eagerly resolve one.
    monkeypatch.setattr(gcp, "find_gcloud", lambda: "gcloud")
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "active-proj")

    def fail_if_called(exe, cfg):
        raise AssertionError("scan should not need an active zone")

    monkeypatch.setattr(gcp, "get_active_zone", fail_if_called)
    monkeypatch.setattr(gcp, "list_instances", lambda exe, project, **k: [])
    assert cli.main(["scan"]) == 0


def test_build_context_prefers_explicit_zone_over_active_config(monkeypatch):
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "proj")

    def fail_if_called(exe, cfg):
        raise AssertionError("should not consult active zone when --zone was passed")

    monkeypatch.setattr(gcp, "get_active_zone", fail_if_called)
    args = cli.build_parser().parse_args(["--zone", "explicit-zone", "status"])
    ctx = cli._build_context(args, "gcloud")
    assert ctx.zone == "explicit-zone"


def test_build_context_falls_back_to_active_zone(monkeypatch):
    monkeypatch.setattr(gcp, "get_active_project", lambda exe, cfg: "proj")
    monkeypatch.setattr(gcp, "get_active_zone", lambda exe, cfg: "gcloud-config-zone")
    args = cli.build_parser().parse_args(["status"])
    ctx = cli._build_context(args, "gcloud")
    assert ctx.zone == "gcloud-config-zone"


# ---------------------------------------------------------------------------
# _remote_setup_command
# ---------------------------------------------------------------------------


def test_remote_setup_command_embeds_valid_ini_config():
    # gns3_server.conf is INI (gns3-server parses it with configparser), not
    # JSON — see _remote_setup_command's comment for why that distinction
    # is load-bearing.
    command = cli._remote_setup_command(user="admin", password="p@ss")
    opener = "<<'GNS3_SERVER_CONF_EOF'\n"
    start = command.index(opener) + len(opener)
    end = command.rindex("\nGNS3_SERVER_CONF_EOF")
    config = configparser.RawConfigParser()
    config.read_string(command[start:end])
    assert config.get("Server", "host") == "0.0.0.0"
    assert config.getboolean("Server", "auth") is True
    assert config.get("Server", "user") == "admin"
    assert config.get("Server", "password") == "p@ss"
    assert config.getint("Server", "console_start_port_range") == cli.CONSOLE_PORT_START
    assert config.getint("Server", "console_end_port_range") == cli.CONSOLE_PORT_END


def test_remote_setup_command_uses_single_quoted_heredoc_delimiter():
    command = cli._remote_setup_command(user="admin", password="pw")
    assert "<<'GNS3_SERVER_CONF_EOF'" in command


def test_remote_setup_command_reports_missing_groups_instead_of_provisioning(tmp_path):
    # Regression test: an enrolled VM (not provisioned by provision.sh) can
    # be missing the docker group entirely. `usermod -aG kvm,docker` used to
    # fail atomically on that under `set -e`, aborting before
    # gns3_server.conf was ever written — confirmed against a real
    # instructor-built VM. Provisioning a VM this tool didn't create is out
    # of scope: the command must detect and report exactly what's missing
    # and exit, not install packages on it and not silently skip the check.
    command = cli._remote_setup_command(user="admin", password="pw")
    assert "usermod -aG kvm,docker" not in command  # the old, atomically-failing form

    script = tmp_path / "setup.sh"
    # Stub out `getent`/`sudo`/`id` on PATH so the group-check branch runs
    # deterministically regardless of what's installed on the machine
    # actually running this test.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "getent").write_text("#!/bin/sh\nexit 1\n")  # every group "missing"
    (fake_bin / "getent").chmod(0o755)
    script.write_text(command)
    result = subprocess.run(
        ["bash", str(script)],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Missing required group(s): kvm docker" in result.stderr
    assert "docker.io" in result.stderr  # actionable recommendation
    assert not (tmp_path / ".config").exists()  # never got to writing the conf


def test_remote_setup_command_is_valid_shell(tmp_path):
    shutil.which("bash") or pytest.skip("bash not available")
    command = cli._remote_setup_command(user="admin", password="p@ss$word")
    script = tmp_path / "setup.sh"
    script.write_text(command)
    result = subprocess.run(["bash", "-n", str(script)])
    assert result.returncode == 0


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
    assert 'setsid nohup "$gns3server_bin"' in command
    assert "echo $!" in command


def test_remote_launch_command_checks_path_then_known_fallback_locations():
    # Regression test: gcloud compute ssh --command is a non-login,
    # non-interactive shell — ~/.bashrc/.profile PATH additions (e.g. from
    # a pipx install) don't apply there, so bare `gns3server` can be
    # genuinely installed but not found. A bare launch attempt reports
    # success either way (nohup backgrounds and returns immediately),
    # making a missing binary invisible until wait_for_http_ready times out
    # 120s later.
    command = cli._remote_launch_command()
    assert "command -v gns3server" in command
    assert "/usr/local/bin/gns3server" in command
    assert '"$HOME/.local/bin/gns3server"' in command


def test_remote_launch_command_fails_fast_when_gns3server_is_nowhere(tmp_path):
    shutil.which("bash") or pytest.skip("bash not available")
    command = cli._remote_launch_command()
    script = tmp_path / "launch.sh"
    script.write_text(command)
    result = subprocess.run(
        ["bash", str(script)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "gns3server not found" in result.stderr
    assert not (tmp_path / "gns3server.pid").exists()


def test_remote_launch_command_uses_fallback_path_when_not_on_path(tmp_path):
    shutil.which("bash") or pytest.skip("bash not available")
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_gns3server = local_bin / "gns3server"
    fake_gns3server.write_text("#!/bin/sh\nsleep 5\n")
    fake_gns3server.chmod(0o755)

    command = cli._remote_launch_command()
    script = tmp_path / "launch.sh"
    script.write_text(command)
    result = subprocess.run(
        ["bash", str(script)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert (tmp_path / "gns3server.pid").exists()
