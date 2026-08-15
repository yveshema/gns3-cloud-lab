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
5000–5020). Firewall allows `tcp:3080,tcp:5000-5020` from the student's
public IPv4 `/32` only. `console_host` is not set. No SSH tunnel — connects
directly to the external IP. (Why the console range must stay open despite
QEMU binding consoles to `127.0.0.1` on the VM is unresolved — see plan §2.7.)
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

- Campus network may filter 5000–5020 — untested, could force an SSH-tunnel
  redesign of `--start`.
- The console-binding mechanism (§2.7) isn't understood, only that the port
  range is load-bearing.
- Whether a `--snapshot`/backup verb is warranted before credit expiry can
  delete a suspended project's resources.
- Whether `--create` belongs in the tool at all, vs. documenting VM creation
  and having the tool adopt an existing one.
- Custom GCE image to avoid every student uploading the OL9 qcow2 — not
  evaluated.

## Needs live-VM validation

Nothing written yet. Add an entry here whenever code is added that assumes
GCP or GNS3-server behavior this container can't check.
