# gns3-2620-lab

Per-student, student-owned GCP fallback for BCIT networking-2620's GNS3 labs.
Primary delivery is local GNS3 on Windows 11; this exists for laptops that
can't run it locally (notably Apple Silicon Macs). Full design and the
empirical findings behind it: `/workspaces/gns3-cloud-plan.md` (mounted
read-only into this container, not part of this repo — if it's missing, ask
for it before touching provisioning logic).

## Status

**No `gcloud` in this dev container.** Nothing here can be live-validated —
no VM, no API calls, nothing. Every constraint below came from a live VM in a
prior session; treat it as authoritative over guesses. When adding code that
touches GCP or a running GNS3 instance, add an entry to "Needs live-VM
validation" below rather than assuming it works.

## Deliverables (target layout)

```
gns3-2620-lab/
├── README.md              student guide, written last, from what actually happened
├── provision.sh           GCE startup script
├── pyproject.toml
└── src/gns3_2620_lab/
    ├── cli.py             --create --start --stop --status
    ├── gcp.py             gcloud subprocess wrappers
    └── gns3conf.py        locate and patch gns3_gui.conf per platform
```

Build order: `provision.sh` first (repeated create/destroy cycles is the
clean-boot test), wrapper second, README last.

## Constraints that code must respect

**Machine type:** nested virtualization needs Intel Haswell+; not available on
E2, N2D, AMD, Arm, or memory-optimized types. N1/N2/C2 only. Default is
`n2-standard-4`. Flag is just `--enable-nested-virtualization` — no custom
image needed.

**VPCS:** must be built from source at tag `v0.6.2` (default branch 0.8.4 is
rejected by gns3-server's gate; Ubuntu 24.04's packaged 0.5b2 is below the
floor). Two independent build fixes, both required:
- truncate `src/getopt.h` (`: > getopt.h`) — GCC 14 errors on its
  non-const `getopt` declaration; glibc's own declaration is sufficient.
- link with `gcc -fcommon` — GCC 10's `-fno-common` default breaks the
  global `vpc` declared in a header.

**uBridge:** not in Ubuntu repos, don't use the GNS3 PPA (version lag, pulls a
conflicting gns3-server). Build from GitHub master →
`/usr/local/bin`, then `setcap cap_net_admin,cap_net_raw=ep`. No `ubridge`
group needed when capabilities are set directly.

**apt fails atomically:** `apt install a b c` aborts entirely if *any* package
has no candidate, and the error only names one of them. Never group packages
whose availability isn't already confirmed; verify installs afterward instead
of trusting the exit code.

**GNS3 config path:** derived from `expanduser("~")` of the user *running the
process*, not the install location. `XDG_CONFIG_HOME` is not honoured. Lands
in `~/.config/GNS3/2.2/`, projects in `~/GNS3/`.

**Networking:** `host = 0.0.0.0`, `auth = True`, console ports bounded (e.g.
5000–5050). Firewall allows `tcp:3080,tcp:5000-5050` from the student's
public IPv4 `/32` only. `console_host` is not set. No SSH tunnel — connects
directly to the external IP. (Why the console range must stay open despite
QEMU binding consoles to `127.0.0.1` on the VM is unresolved — see plan §2.7.)

**Firewall scope is by public IP, not by device.** Anyone sharing the
student's public IP (roommates on the same home router, others on the same
campus/coffee-shop NAT) can also reach the open ports while the rule is
active. The web UI (3080) is protected by `auth = True` plus a per-student
random password regardless; the console ports have no second auth layer of
their own — reachability alone is sufficient. Not a reason to hide the port
numbers (the source-IP restriction is what's actually doing the work, and
portscanning a range you can't route to accomplishes nothing) — but worth
knowing before recommending this to students on shared/public networks.
Public IP lookups must force IPv4 (`curl -4`); an IPv6 result breaks `/32`
CIDR.

**Two addresses change every session, in opposite directions:** the VM's
external IP (ephemeral, changes each start/stop) and the student's public IP
(changes with location). `--start` must refresh both the firewall source
range and the GUI config every time — this is the wrapper's actual reason to
exist, not a convenience.

**`gcloud compute ssh --command` runs a non-login, non-interactive shell:**
`~/.profile` and `~/.bashrc` PATH additions don't apply. Install everything
to `/usr/local/bin`. Group membership from `usermod -aG` doesn't affect the
current session — each `--command` call is a fresh login, so "add group" and
"launch server" must be two separate calls.

**`provision.sh` runs as root at first boot, before any user account
exists.** Anything user-specific belongs in `--start`, not provisioning. It
must be idempotent (sentinel file) and must assert every version/capability
gate itself, exiting non-zero on failure, so a broken provision shows up in
`journalctl -u google-startup-scripts.service` instead of as a mysterious
dead node days later.

**Misc traps:** `hash -r` after replacing a binary at a new path in the same
shell. Instance and disk are separate billable resources; an inline
`--boot-disk-*` disk defaults to `autoDelete: true`. `seq -f` accepts only one
`%` directive. The GCE guest agent creates a local account named after
whoever first runs `gcloud compute ssh` — it doesn't exist at provision time
and the design must not depend on its name. A reserved static IP costs more
idle than the VM saves — don't use one.

## Workflow

- Branch per piece of work.
- Test before commit, to whatever extent is possible without `gcloud` —
  shellcheck, syntax checks, dry runs, unit tests on pure logic. State
  explicitly what couldn't be tested.
- Never push. The user pushes.

## Open questions (from plan §5, still unresolved)

- Campus network may filter 5000–5050 — untested, could force an SSH-tunnel
  redesign of `--start`.
- The console-binding mechanism (§2.7) isn't understood, only that the port
  range is load-bearing.
- Whether a `--snapshot`/backup verb is warranted before credit expiry can
  delete a suspended project's resources.
- Whether `--create` belongs in the tool at all, vs. documenting VM creation
  and having the tool adopt an existing one.
- Custom GCE image to avoid every student uploading the OL9 qcow2 — not
  evaluated.

## Confirmed on a live VM

`provision.sh` completed end-to-end on a genuinely clean `n2-standard-4` /
`ubuntu-2404-lts-amd64` VM (deleted and recreated from scratch, not reused
state from earlier debugging): apt installs, uBridge/VPCS source builds, a
true first-time `pipx install` (not a no-op — "installed package
gns3-server 2.2.61... gns3loopback, gns3server, gns3vmnet now globally
available"), all four assertions, sentinel written, exit 0. Reboot
idempotency also confirmed separately (`gcloud compute instances reset`
with the sentinel left in place hit the fast path correctly). Three real
bugs were found and fixed along the way:

- **`qemu-kvm` is not a real package on `ubuntu-2404-lts-amd64`** — only a
  virtual name `qemu-system-x86` provides. Fixed: install
  `qemu-system-x86` directly.
- **`assert_vpcs` searched for the git tag (`v0.6.2`), not the version
  string `vpcs -v` actually prints (`0.6.2`, no `v`)** — never matched,
  silently correct-looking to a human but failing the automated check.
  Fixed: separate `VPCS_TAG` and `VPCS_VERSION` variables.
- **`dynamips --version` isn't a real flag** — dynamips prints its usage
  banner and exits 1. Under `pipefail` that killed the script via `set -e`
  before `assert_dynamips`'s own error handling ever ran (no `FATAL:`
  line, just an abrupt stop). Fixed: `|| true` on the assignment, deferring
  to the existing `[ -n "$version" ]` check.

The wrapper's `create` command also ran for real against GCP and
succeeded: firewall rule created with the `--rules=tcp:3080,tcp:5000-5020`
syntax as written, instance created with the bundled `provision.sh`
correctly located via `importlib.resources`, `gcp.get_public_ipv4()`
actually reached `api.ipify.org` and got a usable IP, local state written.

## Needs live-VM validation

`provision.sh` — 79 unit tests pass in this container (mocked), and the
above is now confirmed against a real VM. Nothing outstanding.

`src/gns3_2620_lab/` (the wrapper) — `create` is confirmed (above). Not yet
exercised: `start`, `stop`, a repeat `start` (the two-address-refresh path
that's the wrapper's whole reason to exist), or `create` against an
already-existing instance (the idempotent no-op branch). For the GUI-side
items, also needs a real GNS3 client on Windows/macOS:

- **Biggest guess, `cli.py`'s `_remote_setup_command`:** the remote
  `gns3_server.conf` written over SSH is assumed to be JSON shaped like
  `{"Server": {"host": ..., "auth": ..., "console_start_port_range": ...}}`,
  by analogy with `gns3_controller.conf`'s known JSON format. The plan's
  §2.6/§3.3 notes describe server settings as `key = value` prose, which
  could equally mean an INI file read by `configparser`. Never confirmed
  against an actual gns3-server install — if wrong, `--start` will boot the
  server with defaults (`auth = False`? wrong console range?) instead of
  the intended config, and this would not be obviously visible from the
  wrapper's side since `wait_for_http_ready` only checks that *something*
  answers on :3080.
- `gns3conf.gns3_gui_config_path()` — Windows (`%APPDATA%\GNS3\2.2`) and
  macOS (`~/.config/GNS3/2.2`) paths are GNS3's documented layout, not
  verified against a real install on either platform. Only the Linux
  server-side path (§2.6 of the plan) was ever empirically confirmed, and
  that's a different config file (`gns3_server.conf`, not `gns3_gui.conf`).
- `gns3_gui.conf`'s JSON schema — `upsert_remote_server`'s
  `Servers.remote_servers` list with `host`/`port`/`protocol`/`user`/
  `password` keys is a best-effort guess at what the actual GNS3 GUI reads
  on startup, not confirmed against a real client. If wrong, `--start`
  will silently write a file the GUI ignores, and the student's step 6
  ("configure the main server from the printed values") becomes load-
  bearing rather than a formality.
- `_remote_setup_command`'s `sudo usermod -aG kvm,docker` — assumes the
  GCE guest-agent-created account has passwordless sudo (the GCP default,
  but not verified in this repo).
- `gcp.wait_for_http_ready`'s default path `/v2/version` — assumed to
  exist and return a non-5xx status once the server is up; the actual
  gns3-server 2.2.61 API surface wasn't checked.
- `gcp.instance_describe`'s not-found detection (`"not found"` /
  `"NOT_FOUND"` substring match on stderr) — the exact gcloud error text
  for a missing instance wasn't captured from a real call.
- `gcp.get_public_ipv4`'s default service, `api.ipify.org` — reachability
  assumed, not tested from this container (no network egress here either).
- The full `create` → `start` → `stop` → `start` cycle end-to-end,
  including whether the firewall rule syntax
  (`--rules=tcp:3080,tcp:5000-5020`) is accepted as written.
