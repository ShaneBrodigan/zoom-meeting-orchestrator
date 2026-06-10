
# Launch Guide — running a live capture session on AWS

A step-by-step manual for running one labeled Zoom VoIP capture session on the AWS harness.
Follow it top to bottom. First verified end to end on 2026-06-09 (the 2-party call).

> Background / the "why" lives in `REFACTOR_DESIGN.md` and `handoff_zoom_aws_setup.md`.
> This file is just the operational checklist.

---

## 0. The cast (who does what)

| VM  | Private IP   | Role in a run                                  | Runs in Docker? |
|-----|--------------|------------------------------------------------|-----------------|
| VM4 | `10.0.0.7`   | Orchestrator + tshark capture (the conductor)  | **No** — native |
| VM1 | `10.0.1.119` | Zoom **host** bot                              | Yes (SDK image) |
| VM2 | `10.0.2.67`  | Zoom **joiner** bot                            | Yes (SDK image) |
| VM3 | `10.0.3.53`  | Zoom **joiner** bot (3-party runs)             | Yes (SDK image) |
| VM5 | `10.0.4.16`  | iperf noise (later phase)                      | Yes (later)     |

**Two scripts drive a run** (both already on the VMs, in `~/zoom-meeting-orchestrator`):
- `live_check_orchestrator.py` — run on **VM4**. Its `roster=[…]` decides who's in the call.
- `live_check_agent.py` — run on **each client VM**, inside the SDK container.

**Golden rule of ordering:** start the **client agents first** (they begin polling), **then** start the
orchestrator on VM4. The agents ignore any session that already existed when they started, so if VM4
publishes the spec before an agent is up, that agent will miss the call.

---

## 1. Start the VMs

In the AWS console, start the VMs you need:
- **2-party run:** VM4, VM1, VM2
- **3-party run:** VM4, VM1, VM2, VM3

Give them ~30s to boot. SSH in from your laptop via VM4 (the bastion); from VM4 you can reach the
clients with the configured aliases `ssh vm1` / `ssh vm2` / `ssh vm3`.

### 1a. Confirm clocks are synced (do this on every VM)
Labels join the pcap (VM4 clock) to bot heartbeats (client clocks) by timestamp, so the clocks must
agree.
```
chronyc tracking
```
Look for `Reference ID` = `169.254.169.123` (AWS Time Sync), `Leap status : Normal`, and a System-time
offset in micro/milliseconds. If `chronyc` is missing: `sudo systemctl status chrony`.

---

## 2. Set the roster (VM4) — only if changing party size

The call's participants are the `roster` in `live_check_orchestrator.py` on **VM4**
(`~/zoom-meeting-orchestrator`). Edit it (e.g. `nano live_check_orchestrator.py`) so it lists exactly
the VMs you started:

**2-party:**
```python
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=ROLE_HOST),    # VM1
            RosterEntry(ip="10.0.2.67", zoom_role=ROLE_JOINER),   # VM2
        ],
```
**3-party:**
```python
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=ROLE_HOST),    # VM1
            RosterEntry(ip="10.0.2.67", zoom_role=ROLE_JOINER),   # VM2
            RosterEntry(ip="10.0.3.53", zoom_role=ROLE_JOINER),   # VM3
        ],
```
Exactly one `ROLE_HOST`. `participant_count` is derived automatically.

> A VM whose IP is **not** in the roster will still poll but stay idle (it never joins) — useful as a
> negative control, harmless otherwise.

---

## 3. Start the client agents (VM1, VM2, and VM3 for 3-party)

Do this on **each** client VM. The IP changes per VM (see the table) — everything else is identical.

### 3a. One-time setup per VM (skip if already done)
In `~/zoom-meeting-orchestrator`:
```
# make sure the agent driver is present (it is untracked; copy from VM4 if missing):
ls live_check_agent.py
#   if missing, from VM4:  scp live_check_agent.py vm1:~/zoom-meeting-orchestrator/

# make sure .env has the SDK bot creds:
grep -c ZOOM_APP_CLIENT_ID .env        # should print 1

# build the SDK image (takes ~4-5 min; only needed once per VM, image persists):
docker build -t zoom-agent .
```

### 3b. Launch the agent (every run)
Enter the container with host networking (one line):
```
docker run --rm -it --network host -v "$PWD":/tmp/py-zoom-meeting-sdk -w /tmp/py-zoom-meeting-sdk zoom-agent bash
```
Then **inside the container**, install deps and start the agent with **this VM's IP**:
```
pip install boto3 zoom-meeting-sdk
```
- VM1 (host):   `AGENT_IP=10.0.1.119 python live_check_agent.py`
- VM2 (joiner): `AGENT_IP=10.0.2.67  python live_check_agent.py`
- VM3 (joiner): `AGENT_IP=10.0.3.53  python live_check_agent.py`

Each should settle at:
```
[agent] my private IP = 10.0.1.119
[agent] priming existing sessions (these will be skipped)...
[agent] polling S3 for a new spec... (start the orchestrator on VM4 now)
```
Leave every client sitting at `polling…` before moving on.

> `--network host` is required: it lets boto3 reach the instance-role credentials (via IMDS) and makes
> the auto-detected IP correct. `AGENT_IP` is set explicitly anyway as a safety net.
> **Harmless noise you'll see:** ALSA `Invalid CTL` / `Unknown PCM`, `lspci: not found`,
> `QImage … null image`. The bot uses the SDK's virtual audio source, so a missing sound card is fine.

---

## 4. Run the session (VM4)

Once all client agents are polling, on **VM4** (`~/zoom-meeting-orchestrator`):

### 4a. One-time setup (skip if already done)
```
grep -c ZOOM_S2S_ACCOUNT_ID .env                              # should print 1
python3 -c "import boto3, requests, dotenv; print('deps ok')" # if dotenv errors:
sudo apt-get install -y python3-dotenv
```

### 4b. Launch the orchestrator
```
sudo -E python3 live_check_orchestrator.py
```
It prints a `session_id` and the seeded timing (`preroll | duration | postroll`), then runs for ~that
long. The client agent terminals should print `new session … -> forking bot`. When it finishes it
prints the manifest summary (`joins_leaves` with real timestamps) and the S3 paths.

`sudo` is required — tshark captures on `ens5`, and the pcap is written under `/tmp` (dumpcap drops
privileges), so the upload step needs root to read it.

---

## 5. Verify the capture

**The manifest** (substitute your `session_id`):
```
aws s3 cp s3://zoom-bot-dataset-s3/sessions/<session_id>/manifest.json -
```
Check `joins_leaves` has a real `t_join`/`t_leave` for **every** rostered IP (not `null`).

**The pcap** (on VM4, the local `/tmp` copy):
```
ls -lh /tmp/<session_id>.pcap
sudo /usr/bin/tshark -r /tmp/<session_id>.pcap | wc -l
sudo /usr/bin/tshark -r /tmp/<session_id>.pcap -T fields -e ip.src | sort | uniq -c | sort -rn | head
```
Healthy result: tens of thousands of packets; each rostered client IP (`10.0.1.119`, `10.0.2.67`,
`10.0.3.53`) present as a source (the bots' outbound audio), plus Zoom relay IPs as sources (the
downlink). No `10.0.0.7` and no SSH — that confirms the clean pre-NAT capture.

---

## 6. Clean up

**On VM4** — delete the local pcap (the S3 copy is kept as your dataset sample) and confirm no capture
is still running:
```
sudo rm -f /tmp/sess-*.pcap
pgrep -a tshark; pgrep -a dumpcap        # should print nothing
```
**On each client VM** — `Ctrl-C` the agent, then `exit` the container (the `--rm` flag deletes it).

**Finally** — stop all VMs in the AWS console (running ≈ \$93/month; stopped ≈ \$9/month).

> Leaving the client agents running is fine if you want to fire several orchestrator runs back-to-back —
> they keep polling and will pick up each new session automatically.

---

## Troubleshooting quick reference

| Symptom | Fix |
|---|---|
| `docker: invalid reference format` | You pasted the multi-line `docker run` with `\` — use it as one line. |
| boto3 `Unable to locate credentials` | Container isn't reaching IMDS — confirm `--network host` is on `docker run`. |
| Agent matches no role / never forks | Wrong `AGENT_IP`, or this IP isn't in the VM4 roster. Check both. |
| `live_check_agent.py: No such file` | It's untracked — `scp` it from VM4 (`scp live_check_agent.py vm1:~/zoom-meeting-orchestrator/`). |
| `dumpcap … Permission denied` | pcap path must be under `/tmp` (it is) and VM4 run under `sudo`. |
| Agent "won't stop" | Expected — it's a forever-poller. `Ctrl-C` it. The bot child already exited at meeting-end. |

> **Tip:** to skip the per-run `pip install boto3 zoom-meeting-sdk` inside the container, add those two
> packages to the `Dockerfile`'s pip line and rebuild `zoom-agent` once.