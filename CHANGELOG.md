# Changelog

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
  seen in a real terminal.
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
