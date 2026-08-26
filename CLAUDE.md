# gns3-cloud-lab

Per-user, user-owned GCP fallback for BCIT networking-2620's GNS3 labs.
Primary delivery is local GNS3 on Windows 11; this exists for laptops that
can't run it locally (notably Apple Silicon Macs) or need more RAM.

## Status

**No `gcloud` in this dev container.** Nothing here can be live-validated —
no VM, no API calls, nothing. When adding code that touches GCP or a
running GNS3 instance, add an entry under "Needs live-VM validation" in
CHANGELOG.md rather than assuming it works.

## Deliverables (target layout)

```
gns3-cloud-lab/
├── README.md              user guide, written last, from what actually happened
├── install.py             user-facing: bootstrap uv if needed, install the CLI
├── uninstall.py           user-facing: uv tool uninstall
├── provision.sh           GCE startup script
├── pyproject.toml
└── src/gns3_cloud_lab/
    ├── cli.py             scan (default) / create / enroll / start (alias: refresh) / stop / status
    ├── gcp.py             gcloud subprocess wrappers
    └── gns3conf.py        locate and patch gns3_gui.conf per platform; GUI-running check
```

Build order: `provision.sh` first (repeated create/destroy cycles is the
clean-boot test), wrapper second, README last.

## Workflow

- Branch per piece of work.
- Test before commit, to whatever extent is possible without `gcloud` —
  shellcheck, syntax checks, dry runs, unit tests on pure logic. State
  explicitly what couldn't be tested.
- Never push. The user pushes.
- Before testing `start` on a personal machine that has its own real GNS3
  install(s), back up `~/.config/GNS3/2.2/` first. `start` writing straight
  to that default path is correct behavior for the tool — it's testing on
  a machine that already has something real there that needs the
  precaution, not the tool.

Implementation constraints, confirmed behavior, and open questions live as
comments next to the code they govern, not here. See CHANGELOG.md.
