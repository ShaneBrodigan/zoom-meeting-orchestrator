
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
| VM5 | `10.0.4.16`  | iperf noise generator (`client/noise.py`)      | **No** — native |

Plus, for a noise run, a separate **`iperf-server`** host outside the VPC (default VPC, EIP
`108.132.222.246`) that VM5's noise targets — it just runs 3 `iperf3 -s` listeners.

**Three scripts drive a run** (all already on the VMs, in `~/zoom-meeting-orchestrator`):
- `live_check_orchestrator.py` — run on **VM4**. Its `roster=[…]` decides who's in the call.
- `live_check_agent.py` — run on **each client VM**, inside the SDK container.
- `client/noise.py` — run on **VM5 natively** (`python3 -m client.noise`) for a *with-noise* run;
  it loops iperf bursts forever, independent of any session (decision 10). Skip it for no-noise.

**Golden rule of ordering:** start the **client agents first** (they begin polling), **then** start the
orchestrator on VM4. The agents ignore any session that already existed when they started, so if VM4
publishes the spec before an agent is up, that agent will miss the call.

---

## 1. Start the VMs

In the AWS console, start the VMs you need:
- **2-party run:** VM4, VM1, VM2
- **3-party run:** VM4, VM1, VM2, VM3
- **with VM5 noise:** also start **VM5** and the separate **`iperf-server`** (EIP `108.132.222.246`)

Give them ~30s to boot. SSH in from your laptop via VM4 (the bastion); from VM4 you can reach the
clients with the configured aliases `ssh vm1` / `ssh vm2` / `ssh vm3`. **VM5 has no alias** — reach it
with `ssh 10.0.4.16` from VM4. The `iperf-server` is *not* behind VM4 — SSH it directly from the laptop
(same key). ⚠️ Check the prompt before running anything: `client/noise.py` belongs **only** on VM5
(`ip-10-0-4-16`).

### 1b. Start the noise generator (VM5) — only for a with-noise run
On **VM5** (`ssh 10.0.4.16` from VM4, then `cd ~/zoom-meeting-orchestrator`):
```
python3 -m client.noise
```
It reads `config/noise.json` from S3 (instance role) and loops seeded iperf bursts at the `iperf-server`
forever — leave it running for the whole capture (it deliberately blankets pre-roll/gaps/post-roll).
You'll see periodic iperf transfer reports. Optional sanity check on VM4 that noise is flowing pre-NAT:
```
sudo tcpdump -i ens5 -n host 10.0.4.16 and not tcp port 22 -c 5    # expect 10.0.4.16 > 108.132.222.246
```
(Prereqs — all done 2026-06-12, see `handoff_zoom_aws_setup.md`: iperf-server listeners up,
`config/noise.json` in S3, VM5 has iperf3+boto3+repo.)

### 1c. Confirm the iperf-server listeners are up — only for a with-noise run
SSH the `iperf-server` directly from your laptop (`108.132.222.246`; it is **not** behind VM4, same key):
```
systemctl is-active iperf3@5201 iperf3@5202 iperf3@5203    # expect three "active"
ss -lntu | grep 520                                        # three bound sockets
```
They are `enabled` systemd services, so a fresh instance start brings them up automatically — but the
box must finish booting first (if VM5's first iperf burst times out, the server usually just wasn't up
yet). If any line says `inactive`/`failed`:
```
sudo systemctl start iperf3@5201 iperf3@5202 iperf3@5203
```
Do **not** start `iperf3 -s` by hand — it collides with the systemd listener on the same port and fails
to bind. (If you released the EIP to save cost while paused, the target IP changed — update
`config/noise.json` in S3 to match.)

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

**For a with-noise run**, also add the VM5 entry (and `from common.s3 import SessionStore` to the imports);
this *records* the noise block from `config/noise.json` into the manifest — it does **not** start noise
(that runs independently on VM5, step 1b):
```python
        noise = SessionStore().read_noise_config().to_noise_block()
        ...
            RosterEntry(ip="10.0.4.16", zoom_role=ROLE_NONE, noise=noise),  # VM5 noise
```
This is currently **committed** in `live_check_orchestrator.py`. ⚠️ **For a no-noise run, remove that
line** — otherwise the manifest claims noise the pcap won't contain.

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

**For a with-noise run**, also expect `10.0.4.16` (noise uplink) and `108.132.222.246` (noise
downlink/reverse) as high-volume sources — iperf will dominate the packet count. Then run the labeler
(see the bottom tip) and confirm the separation held:
```
python3 -c "import json; d=json.load(open('sessions/<session_id>/labels.json')); f=d['flows']; n=[x for x in f if x['label']=='noise']; print('warnings:', d['warnings']); print('noise:', len(n), sorted({x['rule'] for x in n}), sorted({(x['ip_a'],x['ip_b']) for x in n})); print('zoom touching noise ep:', sum(1 for x in f if x['label'].startswith('zoom') and ({'10.0.4.16','108.132.222.246'} & {x['ip_a'],x['ip_b']})))"
```
Pass: `warnings: []`, rule `['noise-vm-to-iperf-server']`, noise endpoints only
`('10.0.4.16','108.132.222.246')`, and `zoom touching noise ep: 0`.

---

## 6. Clean up

**On VM4** — delete the local pcap (the S3 copy is kept as your dataset sample) and confirm no capture
is still running:
```
sudo rm -f /tmp/sess-*.pcap
pgrep -a tshark; pgrep -a dumpcap        # should print nothing
```
**On each client VM** — `Ctrl-C` the agent, then `exit` the container (the `--rm` flag deletes it).

**On VM5 (noise runs)** — `Ctrl-C` the `client/noise.py` loop.

**Finally** — stop all VMs in the AWS console (running ≈ \$93/month; stopped ≈ \$9/month). For a noise
run, also stop **VM5** and the **`iperf-server`**. Note the `iperf-server`'s EIP bills even while stopped
(~\$3.60/mo) — release it if pausing noise work for a while (a new IP would need `config/noise.json` +
`handoff_zoom_aws_setup.md` updated).

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

> **Tip — don't re-scp the drivers every run.** `.env` and `live_check_agent.py` are untracked but live
> on each VM's EBS disk, which **persists across stop/start** — once they're on a VM they stay there.
> If you ever do need to (re)distribute them, fan out from VM4 in one loop instead of copying VM-by-VM:
> ```
> for vm in vm1 vm2 vm3; do scp ~/zoom-meeting-orchestrator/{.env,live_check_agent.py} $vm:~/zoom-meeting-orchestrator/; done
> ```
> (VM4 already has the key + `vm1/vm2/vm3` SSH aliases. Add `vm5` when running noise.)

> **Tip — you can run the labeler on VM4** instead of the laptop (VM4 has S3 via its instance role, so
> no laptop AWS CLI needed). One-time on VM4: `git pull` (brings in `labeler/`; the untracked
> `live_check_*.py`/`.env` don't conflict) and `sudo apt-get install -y python3-scapy` (**pip is absent
> on VM4 — use apt**). Then, from the repo root: download the session
> (`aws s3 cp s3://zoom-bot-dataset-s3/sessions/<id>/ sessions/<id>/ --recursive`) and run
> `python3 -m labeler.derive_labels sessions/<id>`.