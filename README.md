# GNS3 Cloud Lab — BCIT Networking 2620

A per-student, self-owned Google Cloud VM that runs GNS3, for students whose
laptop can't run it locally — most importantly Apple Silicon Macs, which
can't run the x86 network-device images GNS3 relies on at all.

**Not the primary path.** The course's main delivery is GNS3 running locally
on Windows 11. Use this only if that's not an option for you.

> **Status: not yet validated on a live VM.** This tool was built and unit
> tested without access to `gcloud` or a real GCP project. If something in
> this guide doesn't match what you see, that's expected during the first
> real test pass — tell your instructor rather than trying to work around it
> silently, since the fix likely belongs in the tool, not your workflow.

---

## 1. Google Cloud account

1. Create a Google Cloud account and redeem the **$300 / 90-day free
   trial**: https://cloud.google.com/free
   - The 90 days runs from signup, not from first use. Sign up as close to
     the start of term as you reasonably can — a 16-week term is about
     112 days, so the trial will run out around week 13 regardless. Budget
     for that; don't be surprised by it.
   - Eligibility requires never having had a Google Cloud trial before.
2. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).
3. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) —
   used to install this tool, and works the same way on Windows, macOS, and
   Linux.
4. Create a named gcloud configuration and log in:
   ```
   gcloud config configurations create bcit-2620
   gcloud auth login
   gcloud config set project <YOUR_PROJECT_ID>
   ```
   A configuration bundles account + project + zone under one name, so this
   won't interfere with any other Google Cloud project you use.

## 2. Install the wrapper

```
uv tool install <REPO_URL>
```

*(Ask your instructor for `<REPO_URL>` if it's not filled in above — this
repo hasn't been published anywhere yet.)*

This gives you a `gns3-2620-lab` command with four subcommands: `create`,
`start`, `stop`, `status`.

## 3. Create your lab VM

```
gns3-2620-lab create
```

This creates a firewall rule scoped to your current public IP address and a
`n2-standard-4` VM (4 vCPU, 16 GB RAM) that installs and configures GNS3 on
first boot. It takes a few minutes to finish booting and provisioning —
`gns3-2620-lab status` will show you where it's at. You only do this once
for the term; re-running it later is safe and just confirms the VM is
already there.

## 4. Install the GNS3 desktop client

- Windows / Linux: the standard installer from
  [gns3.com](https://gns3.com).
- Apple Silicon Mac: install via pip, and pass `PyQt6` explicitly — it's
  required but not declared as a dependency:
  ```
  pip install gns3-gui PyQt6
  ```

## 5. Start your lab, each session

```
gns3-2620-lab start
```

Every time you start, both your VM's address and your own public IP may
have changed since last time — this command refreshes both: it starts the
VM if it's stopped, updates the firewall rule to allow your current
location, and writes the connection details into your GNS3 client config.
It prints a summary block with the server IP, port, username, and password.

If the GNS3 client doesn't pick up the server automatically, add it by hand:
**Preferences → Server → Remote Servers → Add**, using the values from the
printed summary. Set your console terminal application preference here too
— those are the only manual GUI settings this tool doesn't handle for you.

## 6. Work

Build your topology, run your nodes, open consoles. The web UI at
`http://<IP>:3080/` (shown in the `start` summary) also lets you export a
project file if you need to submit your work — download it from there.

## 7. Stop when you're done

```
gns3-2620-lab stop
```

**Always stop the VM when you're finished for the session.** You're only
billed for compute time while it's running; the disk keeps a small charge
either way (~$3/month), but leaving the VM running unnecessarily is the
single biggest way to burn through your trial credit early.

## Checking on things

```
gns3-2620-lab status
```

Prints whether the VM exists, whether it's running, its current IP, and
(if known) your login details — without starting or stopping anything.

## If something goes wrong

- If `create` fails partway through, just run it again — it's safe to
  re-run.
- If the server doesn't come up after `start`, the summary will say so.
  SSH isn't something you're expected to use directly, but if you or your
  instructor need to look closer, the server's own log is at
  `~/gns3server.log` on the VM.
- If your campus network is involved and things behave differently there
  than at home, say so — that's a known open question for this tool (it
  depends on outbound access to a specific port range that some
  institutional networks filter).

## What your trial credit actually costs

At roughly $0.20/hour for the VM plus ~$3/month for the disk, a typical
term's usage (a few hours a week) comes to well under $50 of your $300
credit — there's no need to ration your usage session-to-session. The
trial's 90-day clock, not the dollar amount, is the constraint most
students will actually hit.
