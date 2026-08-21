import json
import subprocess

import pytest

from gns3_2620_lab import gcp


# ---------------------------------------------------------------------------
# find_gcloud
# ---------------------------------------------------------------------------


def test_find_gcloud_prefers_plain_name(monkeypatch):
    monkeypatch.setattr(gcp.shutil, "which", lambda name: "/usr/bin/gcloud" if name == "gcloud" else None)
    assert gcp.find_gcloud() == "/usr/bin/gcloud"


def test_find_gcloud_falls_back_to_cmd_on_windows(monkeypatch):
    monkeypatch.setattr(
        gcp.shutil, "which", lambda name: r"C:\gcloud.cmd" if name == "gcloud.cmd" else None
    )
    assert gcp.find_gcloud() == r"C:\gcloud.cmd"


def test_find_gcloud_missing_raises(monkeypatch):
    monkeypatch.setattr(gcp.shutil, "which", lambda name: None)
    with pytest.raises(gcp.GcpError):
        gcp.find_gcloud()


# ---------------------------------------------------------------------------
# run / run_json
# ---------------------------------------------------------------------------


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_sets_config_env_var(monkeypatch):
    captured = {}

    def fake_run(argv, env, capture_output, text, timeout):
        captured["argv"] = argv
        captured["env"] = env
        return FakeCompletedProcess()

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    gcp.run("gcloud", ["config", "list"], config="bcit-2620")
    assert captured["argv"] == ["gcloud", "config", "list"]
    assert captured["env"]["CLOUDSDK_ACTIVE_CONFIG_NAME"] == "bcit-2620"


def test_run_without_config_does_not_set_env_var(monkeypatch):
    import os

    captured = {}

    def fake_run(argv, env, capture_output, text, timeout):
        captured["env"] = env
        return FakeCompletedProcess()

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    gcp.run("gcloud", ["config", "list"])
    assert "CLOUDSDK_ACTIVE_CONFIG_NAME" not in captured["env"] or captured["env"].get(
        "CLOUDSDK_ACTIVE_CONFIG_NAME"
    ) == os.environ.get("CLOUDSDK_ACTIVE_CONFIG_NAME")


def test_run_raises_on_nonzero_exit_by_default(monkeypatch):
    monkeypatch.setattr(
        gcp.subprocess, "run", lambda *a, **k: FakeCompletedProcess(returncode=1, stderr="boom")
    )
    with pytest.raises(gcp.GcpError, match="boom"):
        gcp.run("gcloud", ["compute", "instances", "list"])


def test_run_check_false_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        gcp.subprocess, "run", lambda *a, **k: FakeCompletedProcess(returncode=1, stderr="boom")
    )
    result = gcp.run("gcloud", ["compute", "instances", "list"], check=False)
    assert result.returncode == 1


def test_run_capture_false_passes_capture_output_false_through(monkeypatch):
    captured = {}

    def fake_run(argv, env, capture_output, text, timeout):
        captured["capture_output"] = capture_output
        return FakeCompletedProcess()

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    gcp.run("gcloud", ["compute", "instances", "create", "x"], capture=False)
    assert captured["capture_output"] is False


def test_run_capture_false_error_message_does_not_claim_stderr_detail(monkeypatch):
    # Nothing was captured, so the message must not imply it has the real
    # stderr text — the user already saw gcloud's actual output live.
    monkeypatch.setattr(
        gcp.subprocess, "run", lambda *a, **k: FakeCompletedProcess(returncode=1, stderr="")
    )
    with pytest.raises(gcp.GcpError, match=r"see gcloud output above"):
        gcp.run("gcloud", ["compute", "instances", "create", "x"], capture=False)


def test_run_timeout_raises_gcperror_by_default(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gcloud", timeout=15)

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    with pytest.raises(gcp.GcpError, match="timed out"):
        gcp.run("gcloud", ["compute", "ssh", "x", "--command", "true"], timeout=15)


def test_run_timeout_check_false_returns_failed_result_instead_of_raising(monkeypatch):
    # Regression test: subprocess.run raises on a timeout instead of
    # returning a CompletedProcess. A check=False caller (e.g.
    # wait_for_ssh_ready's poll loop) must get a result with a nonzero
    # returncode back, like any other failed attempt, not an exception that
    # crashes the whole command. Confirmed live: SSH to a freshly-started
    # VM can go quiet instead of refusing fast.
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gcloud", timeout=15)

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    result = gcp.run("gcloud", ["compute", "ssh", "x", "--command", "true"], check=False, timeout=15)
    assert result.returncode != 0


def test_run_json_parses_stdout(monkeypatch):
    monkeypatch.setattr(
        gcp.subprocess, "run", lambda *a, **k: FakeCompletedProcess(stdout='{"status": "RUNNING"}')
    )
    assert gcp.run_json("gcloud", ["compute", "instances", "describe", "x"]) == {"status": "RUNNING"}


def test_run_json_bad_json_raises(monkeypatch):
    monkeypatch.setattr(gcp.subprocess, "run", lambda *a, **k: FakeCompletedProcess(stdout="not json"))
    with pytest.raises(gcp.GcpError):
        gcp.run_json("gcloud", ["compute", "instances", "describe", "x"])


# ---------------------------------------------------------------------------
# GcpContext
# ---------------------------------------------------------------------------


def make_ctx(**overrides):
    defaults = dict(
        gcloud_exe="gcloud",
        project="proj",
        zone="us-west1-b",
        instance="gns3-lab",
        config_name="bcit-2620",
    )
    defaults.update(overrides)
    return gcp.GcpContext(**defaults)


def test_context_run_passes_config_through(monkeypatch):
    captured = {}

    def fake_run(gcloud_exe, args, config=None, **kwargs):
        captured["config"] = config
        return FakeCompletedProcess()

    monkeypatch.setattr(gcp, "run", fake_run)
    make_ctx().run(["config", "list"])
    assert captured["config"] == "bcit-2620"


# ---------------------------------------------------------------------------
# get_active_project
# ---------------------------------------------------------------------------


def test_get_active_project_returns_value(monkeypatch):
    monkeypatch.setattr(gcp, "run", lambda *a, **k: FakeCompletedProcess(stdout="my-proj\n"))
    assert gcp.get_active_project("gcloud") == "my-proj"


@pytest.mark.parametrize("stdout", ["", "(unset)\n"])
def test_get_active_project_unset_raises(monkeypatch, stdout):
    monkeypatch.setattr(gcp, "run", lambda *a, **k: FakeCompletedProcess(stdout=stdout))
    with pytest.raises(gcp.GcpError):
        gcp.get_active_project("gcloud")


# ---------------------------------------------------------------------------
# get_active_zone
# ---------------------------------------------------------------------------


def test_get_active_zone_returns_value(monkeypatch):
    captured = {}

    def fake_run(exe, args, **kw):
        captured["args"] = args
        return FakeCompletedProcess(stdout="us-west1-b\n")

    monkeypatch.setattr(gcp, "run", fake_run)
    assert gcp.get_active_zone("gcloud") == "us-west1-b"
    assert captured["args"] == ["config", "get-value", "compute/zone"]


@pytest.mark.parametrize("stdout", ["", "(unset)\n"])
def test_get_active_zone_unset_raises(monkeypatch, stdout):
    monkeypatch.setattr(gcp, "run", lambda *a, **k: FakeCompletedProcess(stdout=stdout))
    with pytest.raises(gcp.GcpError):
        gcp.get_active_zone("gcloud")


# ---------------------------------------------------------------------------
# instance_describe / instance_status / instance_external_ip
# ---------------------------------------------------------------------------


def test_instance_describe_not_found_returns_none(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(
        ctx,
        "run",
        lambda *a, **k: FakeCompletedProcess(returncode=1, stderr="ERROR: (gcloud) Could not fetch resource: NOT_FOUND"),
    )
    assert gcp.instance_describe(ctx) is None


def test_instance_describe_other_error_raises(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(
        ctx, "run", lambda *a, **k: FakeCompletedProcess(returncode=1, stderr="PERMISSION_DENIED")
    )
    with pytest.raises(gcp.GcpError):
        gcp.instance_describe(ctx)


def test_instance_status_running(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: {"status": "RUNNING"})
    assert gcp.instance_status(ctx) == "RUNNING"


def test_instance_status_missing_instance(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    assert gcp.instance_status(ctx) is None


def test_instance_external_ip_extracts_nat_ip(monkeypatch):
    ctx = make_ctx()
    info = {
        "networkInterfaces": [
            {"accessConfigs": [{"natIP": "34.1.2.3"}]}
        ]
    }
    monkeypatch.setattr(gcp, "instance_describe", lambda c: info)
    assert gcp.instance_external_ip(ctx) == "34.1.2.3"


def test_instance_external_ip_stopped_vm_has_no_nat_ip(monkeypatch):
    ctx = make_ctx()
    info = {"networkInterfaces": [{"accessConfigs": []}]}
    monkeypatch.setattr(gcp, "instance_describe", lambda c: info)
    assert gcp.instance_external_ip(ctx) is None


def test_instance_external_ip_no_instance(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(gcp, "instance_describe", lambda c: None)
    assert gcp.instance_external_ip(ctx) is None


# ---------------------------------------------------------------------------
# list_instances / instance_zone_name
# ---------------------------------------------------------------------------


def test_list_instances_builds_expected_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(gcp, "run_json", lambda exe, args, **kw: captured.update(args=args, kw=kw) or [])
    gcp.list_instances("gcloud", "proj", config="cfg")
    assert captured["args"] == ["compute", "instances", "list", "--project", "proj"]
    assert captured["kw"]["config"] == "cfg"


def test_instance_zone_name_extracts_bare_name_from_url():
    info = {"zone": "https://www.googleapis.com/compute/v1/projects/proj/zones/us-west1-b"}
    assert gcp.instance_zone_name(info) == "us-west1-b"


# ---------------------------------------------------------------------------
# firewall rules
# ---------------------------------------------------------------------------


def test_create_firewall_rule_builds_expected_args(monkeypatch):
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **k: captured.setdefault("args", args))
    gcp.create_firewall_rule(
        ctx, name="gns3-lab-fw", tags=["gns3"], source_range="1.2.3.4/32", ports=["tcp:3080", "tcp:5000-5020"]
    )
    args = captured["args"]
    assert "gns3-lab-fw" in args
    assert "--rules=tcp:3080,tcp:5000-5020" in args
    assert "--source-ranges=1.2.3.4/32" in args
    assert "--target-tags=gns3" in args


def test_update_firewall_source_range_only_touches_source_ranges(monkeypatch):
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **k: captured.setdefault("args", args))
    gcp.update_firewall_source_range(ctx, name="gns3-lab-fw", source_range="5.6.7.8/32")
    args = captured["args"]
    assert "--source-ranges=5.6.7.8/32" in args
    assert not any(a.startswith("--rules") for a in args)
    assert not any(a.startswith("--target-tags") for a in args)


@pytest.mark.parametrize("returncode,expected", [(0, True), (1, False)])
def test_firewall_rule_exists(monkeypatch, returncode, expected):
    ctx = make_ctx()
    monkeypatch.setattr(ctx, "run", lambda *a, **k: FakeCompletedProcess(returncode=returncode))
    assert gcp.firewall_rule_exists(ctx, "gns3-lab-fw") is expected


# ---------------------------------------------------------------------------
# create_instance
# ---------------------------------------------------------------------------


def test_create_instance_builds_expected_args(monkeypatch, tmp_path):
    ctx = make_ctx()
    script = tmp_path / "provision.sh"
    script.write_text("#!/usr/bin/env bash\n")
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        captured["kwargs"] = kw

    monkeypatch.setattr(ctx, "run", fake_run)

    result = gcp.create_instance(ctx, tags=["gns3"], startup_script_path=script)

    args = captured["args"]
    assert "gns3-lab" in args
    assert "--enable-nested-virtualization" in args
    assert "--machine-type=n2-standard-4" in args
    assert "--image-family=ubuntu-2404-lts-amd64" in args
    assert "--boot-disk-size=30GB" in args
    assert f"--metadata-from-file=startup-script={script}" in args
    assert "--tags=gns3" in args
    assert "--format=json" not in args  # nothing parses the result
    # capture=False: this call is slow, and gcloud has its own progress
    # spinner for it — let that show live instead of buffering silently.
    assert captured["kwargs"]["capture"] is False
    assert result is None


def test_create_instance_uses_bundled_script_by_default(monkeypatch):
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **kw: captured.update(args=args))
    gcp.create_instance(ctx, tags=["gns3"])
    startup_arg = next(a for a in captured["args"] if a.startswith("--metadata-from-file=startup-script="))
    bundled_path = startup_arg.split("=", 2)[2]
    assert bundled_path.endswith("provision.sh")


# ---------------------------------------------------------------------------
# start/stop
# ---------------------------------------------------------------------------


def test_start_instance_args(monkeypatch):
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **k: captured.update(args=args, kwargs=k))
    gcp.start_instance(ctx)
    assert captured["args"] == [
        "compute", "instances", "start", "gns3-lab", "--project", "proj", "--zone", "us-west1-b"
    ]
    assert captured["kwargs"]["capture"] is False


def test_stop_instance_args(monkeypatch):
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **k: captured.update(args=args, kwargs=k))
    gcp.stop_instance(ctx)
    assert captured["args"] == [
        "compute", "instances", "stop", "gns3-lab", "--project", "proj", "--zone", "us-west1-b"
    ]
    assert captured["kwargs"]["capture"] is False


# ---------------------------------------------------------------------------
# wait_for_status
# ---------------------------------------------------------------------------


def test_wait_for_status_returns_once_reached(monkeypatch):
    statuses = iter(["PROVISIONING", "STAGING", "RUNNING"])
    monkeypatch.setattr(gcp, "instance_status", lambda ctx: next(statuses))
    clock = {"t": 0.0}
    result = gcp.wait_for_status(
        make_ctx(),
        "RUNNING",
        timeout=100,
        poll_interval=1,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
    )
    assert result == "RUNNING"


def test_wait_for_status_calls_on_tick_once_per_poll(monkeypatch):
    statuses = iter(["PROVISIONING", "STAGING", "RUNNING"])
    monkeypatch.setattr(gcp, "instance_status", lambda ctx: next(statuses))
    clock = {"t": 0.0}
    ticks = []
    gcp.wait_for_status(
        make_ctx(),
        "RUNNING",
        timeout=100,
        poll_interval=1,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
        on_tick=lambda: ticks.append(True),
    )
    assert len(ticks) == 2  # two polls before the third call finds RUNNING


def test_wait_for_status_times_out(monkeypatch):
    monkeypatch.setattr(gcp, "instance_status", lambda ctx: "STAGING")
    clock = {"t": 0.0}

    def fake_sleep(s):
        clock["t"] += s

    with pytest.raises(gcp.GcpError, match="timed out"):
        gcp.wait_for_status(
            make_ctx(),
            "RUNNING",
            timeout=10,
            poll_interval=3,
            sleep=fake_sleep,
            now=lambda: clock["t"],
        )


# ---------------------------------------------------------------------------
# wait_for_external_ip
# ---------------------------------------------------------------------------


def test_wait_for_external_ip_returns_once_available(monkeypatch):
    ips = iter([None, None, "34.1.2.3"])
    monkeypatch.setattr(gcp, "instance_external_ip", lambda ctx: next(ips))
    clock = {"t": 0.0}
    ticks = []
    result = gcp.wait_for_external_ip(
        make_ctx(),
        timeout=60,
        poll_interval=1,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
        on_tick=lambda: ticks.append(True),
    )
    assert result == "34.1.2.3"
    assert len(ticks) == 2


def test_wait_for_external_ip_times_out_returns_none(monkeypatch):
    monkeypatch.setattr(gcp, "instance_external_ip", lambda ctx: None)
    clock = {"t": 0.0}
    result = gcp.wait_for_external_ip(
        make_ctx(),
        timeout=5,
        poll_interval=2,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
    )
    assert result is None


# ---------------------------------------------------------------------------
# ssh_run
# ---------------------------------------------------------------------------


def test_ssh_run_builds_expected_args(monkeypatch):
    ctx = make_ctx()
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(ctx, "run", fake_run)
    gcp.ssh_run(ctx, "echo hi", timeout=30)
    assert captured["args"] == [
        "compute", "ssh", "gns3-lab",
        "--project", "proj", "--zone", "us-west1-b",
        "--command", "echo hi",
    ]
    assert captured["kwargs"]["timeout"] == 30


def test_ssh_run_defaults_to_check_true(monkeypatch):
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **kw: captured.update(kw))
    gcp.ssh_run(ctx, "echo hi")
    assert captured["check"] is True


def test_ssh_run_check_false_is_passed_through(monkeypatch):
    # Needed by cli._discover_or_generate_credentials, which probes for a
    # config file that may not exist on the remote instance and must not
    # raise just because the probe command itself fails.
    ctx = make_ctx()
    captured = {}
    monkeypatch.setattr(ctx, "run", lambda args, **kw: captured.update(kw))
    gcp.ssh_run(ctx, "cat maybe-missing", check=False)
    assert captured["check"] is False


# ---------------------------------------------------------------------------
# wait_for_ssh_ready
# ---------------------------------------------------------------------------


def test_wait_for_ssh_ready_succeeds_once_sshd_accepts_connections(monkeypatch):
    # Regression test: GCE reporting RUNNING doesn't mean sshd is up yet —
    # confirmed on a real VM where a "Connection refused" was immediately
    # followed by success on a bare retry.
    results = iter([FakeCompletedProcess(returncode=255), FakeCompletedProcess(returncode=0)])
    monkeypatch.setattr(gcp, "ssh_run", lambda ctx, cmd, **k: next(results))
    clock = {"t": 0.0}
    ticks = []
    assert gcp.wait_for_ssh_ready(
        make_ctx(),
        timeout=60,
        poll_interval=1,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
        on_tick=lambda: ticks.append(True),
    ) is True
    assert len(ticks) == 1


def test_wait_for_ssh_ready_never_calls_ssh_run_with_check_true(monkeypatch):
    # A "Connection refused" must not raise (GcpError, via check=True) —
    # it's an expected, retryable state while sshd is still starting.
    captured = {}
    monkeypatch.setattr(
        gcp, "ssh_run", lambda ctx, cmd, **k: captured.update(k) or FakeCompletedProcess(returncode=0)
    )
    gcp.wait_for_ssh_ready(make_ctx())
    assert captured["check"] is False


def test_wait_for_ssh_ready_survives_a_slow_attempt_that_times_out(monkeypatch):
    # Regression test, confirmed live: a freshly-started VM's sshd can go
    # quiet for longer than one attempt's timeout instead of refusing fast
    # — subprocess.run raises subprocess.TimeoutExpired for that, which
    # used to propagate all the way out of wait_for_ssh_ready and crash the
    # whole command instead of being retried like any other failed attempt.
    # Exercises the real ssh_run -> GcpContext.run -> run() path, not a
    # mocked ssh_run, since the bug was in that chain's exception handling.
    attempts = iter(
        [
            subprocess.TimeoutExpired(cmd="gcloud", timeout=15),
            FakeCompletedProcess(returncode=0),
        ]
    )

    def fake_run(*a, **k):
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(gcp.subprocess, "run", fake_run)
    clock = {"t": 0.0}
    assert gcp.wait_for_ssh_ready(
        make_ctx(),
        timeout=60,
        poll_interval=1,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
    ) is True


def test_wait_for_ssh_ready_times_out_returns_false(monkeypatch):
    monkeypatch.setattr(gcp, "ssh_run", lambda ctx, cmd, **k: FakeCompletedProcess(returncode=255))
    clock = {"t": 0.0}
    assert gcp.wait_for_ssh_ready(
        make_ctx(),
        timeout=5,
        poll_interval=2,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
    ) is False


# ---------------------------------------------------------------------------
# get_public_ipv4
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode()

    def read(self):
        return self._body


class FakeIPv4Connection:
    last_request_host = None

    def __init__(self, host, timeout=10):
        self.host = host
        self.timeout = timeout
        type(self).last_request_host = host

    def request(self, method, path):
        self._method = method
        self._path = path

    def getresponse(self):
        return FakeResponse(200, "203.0.113.7")

    def close(self):
        pass


def test_get_public_ipv4_returns_validated_ip():
    ip = gcp.get_public_ipv4(connection_cls=FakeIPv4Connection)
    assert ip == "203.0.113.7"


class FakeBadStatusConnection(FakeIPv4Connection):
    def getresponse(self):
        return FakeResponse(500, "")


def test_get_public_ipv4_non_200_raises():
    with pytest.raises(gcp.GcpError):
        gcp.get_public_ipv4(connection_cls=FakeBadStatusConnection)


class FakeNonIPv4Connection(FakeIPv4Connection):
    def getresponse(self):
        return FakeResponse(200, "2001:db8::1")


def test_get_public_ipv4_rejects_ipv6_looking_response():
    with pytest.raises(gcp.GcpError):
        gcp.get_public_ipv4(connection_cls=FakeNonIPv4Connection)


# ---------------------------------------------------------------------------
# wait_for_http_ready
# ---------------------------------------------------------------------------


class FakeHttpConnection:
    def __init__(self, host, port, timeout=5):
        self.host = host
        self.port = port

    def request(self, method, path):
        pass

    def getresponse(self):
        return FakeResponse(200, "")

    def close(self):
        pass


class FlakyThenOkConnection:
    calls = 0

    def __init__(self, host, port, timeout=5):
        pass

    def request(self, method, path):
        type(self).calls += 1
        if type(self).calls < 3:
            raise OSError("connection refused")

    def getresponse(self):
        return FakeResponse(200, "")

    def close(self):
        pass


class AlwaysDownConnection:
    def __init__(self, host, port, timeout=5):
        raise OSError("connection refused")


def test_wait_for_http_ready_success_first_try():
    assert gcp.wait_for_http_ready("1.2.3.4", 3080, connection_cls=FakeHttpConnection) is True


def test_wait_for_http_ready_retries_then_succeeds():
    FlakyThenOkConnection.calls = 0
    clock = {"t": 0.0}
    ticks = []
    result = gcp.wait_for_http_ready(
        "1.2.3.4",
        3080,
        timeout=60,
        poll_interval=1,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
        connection_cls=FlakyThenOkConnection,
        on_tick=lambda: ticks.append(True),
    )
    assert result is True
    assert len(ticks) >= 1


def test_wait_for_http_ready_times_out_returns_false():
    clock = {"t": 0.0}
    result = gcp.wait_for_http_ready(
        "1.2.3.4",
        3080,
        timeout=5,
        poll_interval=2,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
        connection_cls=AlwaysDownConnection,
    )
    assert result is False
