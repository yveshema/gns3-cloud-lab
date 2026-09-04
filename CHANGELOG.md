# Changelog

## 2026-09-04

### Added
- `provision.sh` installs libvirt, so a GNS3 NAT node works. The NAT node
  is a front end for libvirt's `default` network (virbr0,
  192.168.122.0/24), and libvirt was never installed at all — confirmed on
  the real VM: no virbr0, no libvirt units. New packages, in this order:
  `dnsmasq-base`, `libvirt-daemon-system`, `libvirt-clients`, `iptables`.
  The order is load-bearing. `dnsmasq-base` is only a **Recommends** of
  `libvirt-daemon-system` (checked with `apt-cache show`), and
  `apt_install_one` installs with `--no-install-recommends`, so installing
  libvirt on its own produces a `default` network that cannot start —
  visibly identical to libvirt being missing entirely, from a completely
  different cause. Installing dnsmasq first lets libvirt's own postinst
  start the network.
- `apt-mark manual iptables`, which is not the same thing as installing it.
  libvirt's `Depends: iptables | firewalld` is already satisfied by the
  iptables that arrived as `docker.io`'s automatic dependency, so
  `apt_install_one` returns early at its `dpkg -s` check and the
  auto-installed flag is never cleared. Removing Docker some months later
  would then let `autoremove` take iptables with it, and libvirt networks
  would stop starting with nothing on the machine to explain why.
- `ensure_libvirt_network()`, run on every boot and deliberately outside
  the sentinel gate: enables `virtnetworkd.service` if that unit exists,
  otherwise `libvirtd.service` (Ubuntu 24.04's libvirt 10.0.0 is still
  monolithic, so the unit is probed for rather than assumed), then
  autostarts and starts the network if it isn't already. Defining the
  network from `/usr/share/libvirt/networks/default.xml` is only a
  fallback: `libvirt-daemon-config-network` is a hard dependency of
  `libvirt-daemon-system` and both ships and autostarts `default`.
- `assert_libvirt()` — fatal if virbr0 lacks 192.168.122.1/24, or if
  `default` is not both active and autostart. It retries briefly first,
  because libvirt is socket-activated and the network may still be coming
  up when the startup script reaches that point. It deliberately does not
  check virbr0's link state: a bridge borrows its carrier from its ports,
  so virbr0 reads DOWN/NO-CARRIER on a perfectly healthy machine with no
  guest attached.
- `report_firewall_state()` — logs the libvirt version, the FORWARD policy
  and the FORWARD/LIBVIRT_FWO/LIBVIRT_FWI rules to the journal on every
  boot. Informational, never fatal. This is insurance for the one thing
  the NAT node depends on that this script does not control, described
  below.
- `--dry-run`. Every mutating command routes through a `run()` wrapper that
  prints instead of executing, and the apt work is simulated with
  `apt-get install -s` — apt's own resolver running for real against the
  real package lists, rather than a guess about what it would do. Read-only
  probes deliberately don't go through the wrapper, so a dry run still
  reports which guards would fire. Assertions are skipped under `--dry-run`
  and say so, since they check the result of steps that didn't run. A flag
  rather than an environment variable because GCE runs startup scripts with
  no argv at all, so the flag can only ever arrive from a human or from
  `upgrade --dry-run` running the script over SSH.
- `upgrade` CLI command (with `--dry-run`), for re-running today's
  `provision.sh` against an already-provisioned, running VM without a full
  `stop`/`create` cycle. Refusal order: GNS3 running locally (upgrading
  stops the gns3server the GUI may be connected to, dropping a live lab
  session with no warning) → no local state record → VM not RUNNING (this
  command deliberately does not boot the VM itself — it needs SSH access to
  invoke `google_metadata_script_runner startup` directly, since GCE only
  re-runs the startup-script metadata key on boot, not on a live metadata
  update) → no `/var/lib/gns3-cloud-lab/provisioned` sentinel on the VM
  (checked even under `--dry-run`, since a dry run of a command that
  doesn't apply would be misleading). A real run then stops gns3server via
  its `~/gns3server.pid` convention (SIGTERM, then SIGKILL if still alive
  after a brief wait — `upgrade` owns this process's lifecycle, so killing
  it automatically is safe) and checks for an orphaned `ubridge` afterward;
  finding one refuses and reports it rather than killing it, since an
  orphan surviving gns3server's own graceful shutdown is unexpected, not a
  routine "someone's using it" case, and ubridge holds
  `cap_net_admin`/`cap_net_raw` with no guarantee blind killing leaves
  kernel-side state clean. `--dry-run` instead uploads today's local
  `provision.sh` and runs it in place with `sudo bash ... --dry-run`,
  touching nothing else. A real run pushes today's `provision.sh` as the
  instance's startup-script metadata, clears the sentinel, then invokes
  `sudo google_metadata_script_runner startup` over SSH with a 600s
  timeout (a version mismatch can trigger a from-source rebuild of
  ubridge/VPCS) and reports success/failure from its exit code — it does
  not itself relaunch gns3server, so the user is told to run `start`
  afterward.
- `gcp.ssh_run` (and its Windows batch-shim path, `_ssh_run_via_upload`)
  gains `capture: bool = True`, so `upgrade` can let
  `google_metadata_script_runner`'s output stream live instead of being
  buffered and dumped afterward — same reasoning `create`/`start`/`stop`
  already use `capture=False` for. Default `True` leaves every existing
  caller unaffected. On the upload path, only the final "run the uploaded
  script" SSH call sees the caller's `capture` value; the `scp` upload
  itself is unaffected, same treatment as `check`/`timeout` there already.
- `gcp.add_metadata_startup_script` — `gcloud compute instances
  add-metadata --metadata-from-file=startup-script=...` against an
  existing instance, mirroring `create_instance`'s use of the same flag at
  creation time.
- `gcp.scp_upload_file` and `gcp.bundled_provision_script` — the former
  generalizes the `scp` upload `_ssh_run_via_upload` already did
  internally, for a caller (`upgrade --dry-run`) that needs to run a real
  script by name on the VM rather than a command string; the latter
  publicly exposes the same bundled-`provision.sh` resolution
  `create_instance` already used privately, since `upgrade` also needs
  today's local script.

### Changed
- uBridge is pinned to tag **v1.2.1**. It was a `--depth 1` clone of
  master, which meant `create` installed whatever HEAD was on the day it
  ran. Upstream is already past this (v1.2.1 is commit 9359c6a, and v1.2.2
  exists), so a `create` today would have installed a version nothing in
  this repo has ever been validated against, and a different one from what
  a student who ran `create` last week is running. v1.2.1 is what the real
  VM runs and what everything here was checked against. This is a
  behaviour change to `create`, not only part of the NAT fix. New
  `assert_ubridge_version`, matching `assert_vpcs`.
- `build_ubridge` and `build_vpcs` skip the clone and build when the
  installed binary already reports the wanted version. `build_ubridge`
  re-applies `setcap` even on the skip path: `install` replaces the file
  and drops its capabilities, a hand-placed binary may never have had them,
  and `assert_ubridge` is fatal without them.
- The sentinel now gates only the install steps. The libvirt network setup,
  the firewall report and the assertions run on every boot.

### Fixed
- `install_gns3_server` killed the entire script on any second run.
  `pipx install` exits non-zero when the package is already present, and
  under `set -euo pipefail` that took out everything after it — which is
  what made re-running `provision.sh` over an existing VM impossible,
  sentinel or no sentinel. It now compares
  `/usr/local/bin/gns3server --version` against the pin, skips when they
  match, and passes `--force` only on a real mismatch. The version probe
  fails open: an unparseable or absent banner counts as a mismatch, so the
  cost of an unexpected output format is a reinstall, never a wrong skip.

### Ruled out, with the evidence
- **No `firewall_backend` setting in `/etc/libvirt/network.conf`.** That
  setting arrived in libvirt 10.4; Ubuntu 24.04 ships 10.0.0-2ubuntu8.16,
  where it does not exist. Do not add it.
- **No DOCKER-USER workaround unit.** Docker (docker.io 29.1.3) does set
  `-P FORWARD DROP` and does install DOCKER-USER and DOCKER-FORWARD, but on
  Ubuntu 24.04 it does not block libvirt guest egress, confirmed live on
  the VM. The FORWARD chain jumps to libvirt's own chains *before*
  Docker's:

      -A FORWARD -j LIBVIRT_FWX / LIBVIRT_FWI / LIBVIRT_FWO
      -A FORWARD -j DOCKER-USER
      -A FORWARD -j DOCKER-FORWARD

  and those chains carry `-s 192.168.122.0/24 -i virbr0 -j ACCEPT`
  (LIBVIRT_FWO) and the matching RELATED,ESTABLISHED rule inbound
  (LIBVIRT_FWI). ACCEPT in a user-defined chain is a terminating verdict,
  so guest traffic never reaches Docker's chains and the DROP policy never
  applies to it. A Fedora workstation behaves differently because its
  libvirt 12 uses the nftables backend — a different platform, not this
  one. `report_firewall_state()` exists precisely because this ordering is
  not something this script sets: if a future Docker or libvirt package
  reorders it, the journal says so on the next boot instead of the
  breakage surfacing weeks later as "the NAT node can't reach the
  internet".

### Not faults — don't code around them
- virbr0 reads DOWN/NO-CARRIER while no ports are attached. A bridge takes
  its carrier from its ports.
- dnsmasq shows two processes with identical `argv`. The second is the
  `--dhcp-script` helper. The `--interface=virbr0` binding lives in
  `/var/lib/libvirt/dnsmasq/default.conf`, not on the command line.

### Tested
- `shellcheck` clean and `bash -n` clean.
- 40 new unit tests in `tests/test_provision_sh.py` covering the skip
  guards, install order, the libvirt state machine, the retry loop, the
  firewall report's never-fatal behaviour and `--dry-run`. They source
  `provision.sh` (its last line is now guarded on `BASH_SOURCE`, so
  sourcing gets the functions and no side effects) with stub executables
  ahead of the real ones on PATH, and assert both what ran and what
  didn't. Not bats — it isn't available in the dev container; pytest is,
  and is what the rest of the suite already uses.
- A full `main --dry-run` was walked through against a simulated
  gns3-lab-shaped machine (libvirt absent, uBridge 1.2.1 and VPCS 0.6.2
  already installed, no sentinel): it skips both builds, simulates the
  four new packages, and reports the pipx reinstall — no rebuild of 1.2.1
  over 1.2.1.
- `upgrade`: new unit tests in `tests/test_cli.py` (one per refusal branch
  — GUI running, no state, VM not RUNNING, no sentinel including under
  `--dry-run`, orphaned ubridge after stopping gns3server) and
  `tests/test_gcp.py` (`add_metadata_startup_script` argv assembly,
  `capture` threaded through `ssh_run` and its upload path, regression
  guard that existing `capture=True` callers are unaffected), plus
  call-order assertions for both the `--dry-run` and real-run happy paths
  (real path: gns3server stopped → metadata pushed → sentinel cleared →
  runner invoked, in that order; dry-run path never stops gns3server or
  checks for ubridge at all). Full suite: 241 passed. `ruff check` shows
  only pre-existing findings in files/lines this branch didn't touch (a
  `git diff --stat` confirms every changed file is insertion-only).

### Confirmed live (this session)
- `upgrade`, against a real VM and a real GNS3 GUI: refused while the VM
  was still down (VM not RUNNING), refused once the VM was up but the GUI
  was open, `--dry-run` uploaded and ran `provision.sh --dry-run` over SSH
  without stopping the server or touching metadata, and a real run stopped
  `gns3server`, found no orphaned `ubridge` afterward, pushed today's
  `provision.sh` as the startup script, cleared the sentinel, and
  re-invoked it via `sudo google_metadata_script_runner startup`
  successfully. The pinned SSH user, `test -f`/`sudo rm -f`/`sudo bash
  ... --dry-run` over `gcloud compute ssh --command`, and the `pgrep -x
  ubridge` orphan check's no-orphan path all behaved as expected.
- Not exercised: the test VM's uBridge/VPCS were already pinned to this
  branch's versions, so the run never hit the from-source rebuild path —
  see below for what that leaves open.

### Needs live-VM validation
- All of the above on a real VM. Nothing in this entry has been run against
  GCP; the findings it is built on were observed on the VM, but the code
  written from them has not been.
- That `ubridge -v` prints a parseable `1.2.1`, which both the build skip
  guard and `assert_ubridge_version` depend on. The guard fails open (an
  unrecognised banner rebuilds), but the assertion is fatal.
- That `gns3server --version` prints a parseable version — same shape, and
  the same fail-open guard, but no assertion behind it.
- That `virsh net-info default` labels its fields exactly `Active:` and
  `Autostart:` on libvirt 10.0.0, which `libvirt_net_field` parses on.
- Whether `apt-get install -s` under `--dry-run` gives a usable answer when
  run over SSH as a non-root user; the probes that read machine state
  (`virsh net-info` against qemu:///system in particular) need root, so a
  dry run should be run with `sudo`.
- A VM provisioned before this change has a sentinel and no libvirt, so
  `ensure_libvirt_network` is fatal there on every boot until `upgrade`
  re-provisions it. The message says so. Nothing else in the script runs
  after that point, but the only things after it are the assertions and the
  sentinel write, and `start`/`stop` don't depend on the startup script
  succeeding — so the expected impact is a FATAL line in the journal and
  nothing more. Unconfirmed.
- `upgrade`'s version-mismatch rebuild path: confirmed live above for a
  same-version run (no rebuild triggered), but `sudo
  google_metadata_script_runner startup` re-invoking `provision.sh`'s
  from-source uBridge/VPCS rebuild, and whether the 600s SSH timeout is
  enough headroom for it, remain unconfirmed — the test VM's versions
  already matched today's pins. Also unconfirmed: the orphaned-`ubridge`
  branch of the `pgrep -x ubridge` / `ps -o pid,etimes,cmd` check (only the
  no-orphan path was exercised), since the parsing (`first_pid` from
  line 2) assumes a stable `ps` column layout never checked against an
  actual orphan.

## 2026-08-26

### Added
- `enroll --ssh-user USER` pins the Linux account to SSH as on that specific
  VM, and `start` reuses it from then on. Root cause this addresses:
  `gcloud compute ssh` with no explicit user always resolves its own
  default (local OS account, or the OS Login identity) — confirmed against
  Google's own docs that this is recomputed fresh on every call and never
  persisted anywhere gcloud controls, and that `ssh-keys` metadata governs
  who's *allowed* to connect, not who gets picked when no user is given. On
  a VM set up by hand (not by `provision.sh`), that default account can
  land somewhere other than wherever gns3-server was actually installed —
  auth succeeds, but `~/.config/GNS3/2.2/...` and `gns3server`'s own
  location are read from the wrong home directory. Confirmed live: SSH as
  the default account succeeded and found nothing; SSH as a different,
  explicit account on the same VM found gns3-server already set up.
  `GcpContext.ssh_user`, once set, is applied to *every* `ssh_run` call —
  including both argv-building branches (the plain `--command` path and
  the Windows/multi-line upload path's `scp` + `ssh`) — and `enroll` applies
  it to its own SSH calls (boot-and-wait, credential discovery) immediately
  rather than only saving it for `start` to pick up later, so credential
  discovery doesn't run against the wrong account in the same enroll that
  set it. `start` reads it back from local state (`entry.get("ssh_user")`,
  so a VM enrolled before this change behaves exactly as it did before).

### Fixed
- `start` failed on Windows with `bash: line 1: C:WINDOWSsystem32cmd.exe:
  command not found` the moment it reached `_remote_setup_command`'s
  multi-line `--command`. Root cause: `gcloud` on Windows is `gcloud.cmd`,
  a batch file, which Windows can't launch directly — `CreateProcess`
  silently relaunches it through `cmd.exe /c`, and that relaunch rebuilds
  a single command line out of our whole argv. A single-line argument
  survives the round trip (confirmed individually: spaces, double quotes,
  `$()`, pipes, semicolons), but a real newline does not — cmd.exe has no
  way to represent one, and the argument arrives at the VM scrambled,
  starting with a fragment of cmd.exe's own `COMSPEC` path. Reproduced
  live against a real VM from the Windows side, one character class at a
  time, and the exact reported failure reproduced on command.
  `gcp.ssh_run` now detects a multi-line command bound for the batch shim
  (`gcloud_exe` ending in `.cmd`/`.bat`) and routes it through
  `gcloud compute scp` + `--command "bash <name>"` instead of passing the
  script itself through `--command` — verified end to end against the
  same VM, including that the script's own exit code (not the cleanup
  `rm`'s) is what the caller sees. A real gcloud binary (Linux/macOS)
  never goes through this — argv reaches it directly with no relaunch —
  so that path is unchanged from what was already confirmed live in the
  `_remote_setup_command` INI fix below.
- `start` patched `gns3_gui.conf`/`gns3_server.conf` on Windows too, but
  gns3-gui's GUI never reads those names there — Windows-reported and
  confirmed against gns3-gui's own source (`local_config.py`'s
  `_resetLoadConfig()`, `local_server_config.py`): both files are named
  with a `.ini` extension instead of `.conf` on Windows specifically
  (same directory, same content/format either way — JSON for the gui
  file, INI for the server file). `gns3_gui_config_path()` and
  `gns3_local_server_conf_path()` in `gns3conf.py` now branch on
  `platform.system()` for the filename, same pattern already used by
  `app_config_dir()`. Not live-tested on Windows (see below).
- `create` printed nothing at all while running — reported from a real run,
  and the one command with no feedback of any kind. Every other command
  either announces what it's doing (`start`/`stop`) or ticks a `_progress`
  dot per poll. `create` has no poll loop to tick against: it's four
  one-shot calls in a row (`instances describe`, the public-IP lookup, the
  firewall rule, `instances create`), each a network round-trip, and each
  gcloud invocation additionally costs a couple of seconds of its own
  startup before reaching the API. New `_step()` helper — the non-looping
  companion to `_progress` — announces each one *before* it runs, and
  `start`/`stop`'s existing pre-gcloud lines now route through it too. It
  always flushes: those two, and `create`'s last step, hand the terminal
  straight to a `capture=False` subprocess writing to the same fd, so an
  unflushed announcement can surface *after* the output of the step it was
  announcing whenever stdout isn't a terminal (block-buffered). Regression
  test asserts call order interleaved with output, not just that the text
  appears somewhere — confirmed to fail without the fix.

## 2026-08-21

### Added
- Renamed the package/command from `gns3-2620-lab` to `gns3-cloud-lab`,
  matching the repo name. Console script, `pyproject.toml`, the Python
  package (`gns3_2620_lab` → `gns3_cloud_lab`), the local state directory,
  `provision.sh`'s sentinel directory, and every doc reference all moved
  together. Existing installs: local state doesn't carry over
  automatically — re-run `enroll` once after reinstalling (it correctly
  rediscovers the VM's real credentials rather than generating new ones).
- `install.py` now also sets up `gclab` as a short alias for
  `gns3-cloud-lab`, but only if nothing on the machine already provides
  that name. A real symlink on Linux/macOS; a tiny generated `.cmd` shim
  on Windows, since real symlinks there need Developer Mode or admin
  rights (verified against uv's own docs: Windows tool executables are
  copied, not symlinked, for the same reason). Placed in whatever
  directory `uv tool dir --bin` reports, so it's on PATH under the exact
  same conditions the real command already is. 
  `uninstall.py` removes it again, but only if it
  still points at this tool's own command — never something else that
  happens to be named `gclab`. Verified end-to-end on Linux (create,
  reinstall no-ops, guard against a pre-existing `gclab`, uninstall
  removes ours and leaves an unrelated one alone); the Windows `.cmd` path
  is unit-tested but not yet run on a real Windows machine.

### Changed
- `enroll` no longer discovers credentials against a VM it hasn't confirmed
  is reachable. It now starts the VM first if it isn't already running
  (sharing `start`'s boot-and-wait sequence via a new
  `_boot_and_wait_for_ssh` helper), and refuses to enroll outright if SSH
  never comes up, instead of silently falling through to
  `_discover_or_generate_credentials`'s "nothing configured yet" branch.
  That branch generates a fresh random password — correct for a genuinely
  unconfigured VM, but wrong if the real reason was "couldn't reach it,"
  since a subsequent `start` would then silently overwrite whatever real
  credentials (e.g. an instructor's) were already on it.

### Fixed
- `_remote_setup_command` wrote `gns3_server.conf` as JSON; gns3-server
  actually parses that file with `configparser` (INI), confirmed against
  the real source at the exact pinned tag (`v2.2.61`) and reproduced
  locally (`configparser.RawConfigParser().read_string()` on the generated
  content raises `MissingSectionHeaderError`). gns3-server catches and
  logs that parse failure rather than raising it, so the server started
  anyway with every setting unset, silently falling back to its own
  built-in defaults: auth off (the printed username/password were never
  actually checked — only the per-session firewall rule was gating
  access) and a console port range of 5000-10000, wider than the
  firewall's 5000-5050. Confirmed live: the parse-failure line was in
  `~/gns3server.log`, and `GET /v2/projects` succeeded with no
  credentials. Now writes real INI. `_discover_or_generate_credentials`
  (used by `enroll`) read the same file as JSON and had the same bug —
  fixed to parse INI too.
- `gns3conf.app_config_dir()` ignored `$XDG_CONFIG_HOME`, always using
  `~/.config`. gns3-gui's real `configDirectory()` checks
  `$XDG_CONFIG_HOME` first (verified against its source) — a user with
  that variable set would have this tool patching a config file gns3-gui
  never reads. Fixed to check it too.
- `gcp.run()` let `subprocess.TimeoutExpired` propagate uncaught. Confirmed
  live: a freshly-started VM's sshd went quiet for longer than
  `wait_for_ssh_ready`'s 15s per-attempt timeout instead of refusing fast,
  and the resulting exception crashed the whole `start` command with a raw
  traceback instead of being retried like any other failed attempt — the
  same `check=False` that suppresses a nonzero-exit `GcpError` did nothing
  for a timeout, since `subprocess.run` raises before ever returning a
  result to check. `run()` now catches the timeout: raises `GcpError` when
  `check=True`, returns a failed `CompletedProcess` (returncode 124) when
  `check=False`, so every existing `check=False` caller keeps working via
  its usual returncode check instead of crashing.

### Confirmed live (this session)
- The INI fix above: `~/gns3server.log` showed a fresh "Load configuration
  file" line after `start`, and an unauthenticated
  `GET /v2/projects` returned `401 Unauthorized` instead of `200` — auth is
  enforced. Notably, gns3-server picked this up without restarting: it
  polls its config file for changes (`FileWatcher`, 1s interval) and
  re-fetches config on every single request, so the already-running
  process adopted the corrected file with no VM restart needed.

## 2026-08-15

### Confirmed against real GCP
- `provision.sh`, `create`, `start`, a repeat `start` after `stop` with the
  VM's external IP changed, `enroll`, and a real GNS3 desktop client
  connecting on Linux — all confirmed working end-to-end.
- Fixed: `qemu-kvm` is a virtual package on Ubuntu 24.04 (the real package
  is `qemu-system-x86`); `assert_vpcs` was matching the git tag instead of
  the version string; `assert_dynamips` needed `|| true` since
  `dynamips --version` exits 1 and would otherwise kill the script under
  `set -e`/`pipefail`.
- Fixed: `_remote_launch_command`'s `pgrep -f gns3server` always
  self-matched, because the ssh `--command` string itself contains the
  literal text "gns3server" — replaced with a PID file.
- Fixed: `patch_gui_conf` alone doesn't make the GUI connect to the remote
  server. GNS3's "Remote main server host" field reads a separate
  client-side `gns3_server.conf` (INI), not `gns3_gui.conf`'s
  `Servers.remote_servers`. Added `patch_local_server_conf` to write it.
- Enrolled VMs can be missing pieces `provision.sh` would have installed (a
  real instructor-built VM had `kvm` but not `docker.io`); `start` detects
  missing `kvm`/`docker` groups and reports exactly what's missing instead
  of installing anything on a VM this tool didn't create.
- Toggling "Enable local server" in the GUI and back doesn't stick — the
  next `start`/`refresh` unconditionally re-patches both client config
  files, so no manual recovery is needed.

### Needs live-VM validation
- The `.ini`-on-Windows filename fix (2026-08-26 entry above): confirmed
  against gns3-gui's source, not yet against a real GNS3 GUI reading the
  patched file on a Windows machine.
- `enroll --ssh-user`/`start`'s reuse of it (2026-08-26 entry above): the
  `USER@INSTANCE` argv form itself against a real `gcloud compute ssh` and
  `gcloud compute scp`, and that it actually lands `start`'s SSH calls on
  the account with gns3-server installed on the reporter's real
  manually-provisioned VM.
- `status`; `create`'s idempotent already-exists branch and `--dry-run`;
  `scan`; the GUI-must-be-closed gate and config backup/restore around
  `start`/`stop`; `install.py`/`uninstall.py`.
- `_remote_launch_command`'s PATH-fallback resolution (`command -v`, then
  `/usr/local/bin`, then `~/.local/bin`) — added after `wait_for_http_ready`
  timed out on a real instructor-built VM; not yet confirmed as the actual
  cause.
- Progress feedback during `start`/`stop` wait loops, and gcloud's own
  progress output on `create`/`start`/`stop` — unit-tested only, not yet
  seen in a real terminal. Partly answered on 2026-08-26: a real `create`
  run showed no output whatsoever, which is what the `_step()` change above
  addresses. Still unconfirmed live: whether gcloud's *own* progress output
  reaches the terminal through `capture=False` on each platform — if it
  does, `create`'s last step is now announced and then followed by gcloud's
  spinner; if it doesn't, that step is announced and then silent for up to
  a minute, and needs a `_progress`-style ticker on a thread instead.
- `gns3conf.gns3_gui_config_path()` on Windows/macOS — GNS3's documented
  layout, unverified; only Linux has a real client connection behind it.
- `gcp.instance_describe`'s not-found detection (`"not found"`/
  `"NOT_FOUND"` substring match) — exact gcloud error text for a missing
  instance not yet captured.
- `gns3conf.gui_is_running()` — PID-file check and process-name match read
  from GNS3's own source, never observed against a real running GUI.
- `install.py`'s uv-bootstrap path — untested; only the already-have-uv
  path is exercised by unit tests.
- `gcp.instance_zone_name`'s assumption that `compute instances list`
  reports zone as a full resource URL — standard GCE API shape, unconfirmed
  against real output.

### Open questions
- Campus networks may filter 5000-5050 — untested, could force an
  SSH-tunnel redesign of `start`.
- The console-binding mechanism — why the port range must stay open despite
  QEMU binding consoles to `127.0.0.1` on the VM — isn't understood.
- Whether a `--snapshot`/backup verb is warranted before credit expiry
  deletes a suspended project's resources.
- Whether `create` belongs in the tool at all, vs. documenting VM creation
  and having the tool adopt an existing one.
- A custom GCE image to avoid every user uploading the OL9 qcow2 — not
  evaluated.

### Development gotchas
- `uv tool install --force .` can silently reinstall a stale cached build
  with no "Building..." line in the output; `install.py` always passes
  `--force --no-cache` for this reason.
- Reserved static IPs cost more idle than the VM saves — not used.
- The GCE guest agent creates a local account named after whoever first
  runs `gcloud compute ssh`; it doesn't exist at provision time, so nothing
  in this tool depends on its name.
