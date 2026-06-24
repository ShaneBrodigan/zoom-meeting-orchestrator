# Launch Guide — run one capture session

Operational checklist for one labeled Zoom VoIP capture. **Assumes the VMs you need are
already running and you're SSH'd into VM4** (the bastion). From VM4: `ssh vm1` / `ssh vm2` /
`ssh vm3`; the noise VM is `ssh 10.0.4.16` (no alias). Background/"why" lives in
`REFACTOR_DESIGN.md`.

| VM  | IP           | Role                              | Docker? |
|-----|--------------|-----------------------------------|---------|
| VM4 | `10.0.0.7`   | Orchestrator + tshark capture     | native  |
| VM1 | `10.0.1.119` | Zoom **host** bot                 | yes     |
| VM2 | `10.0.2.67`  | Zoom **joiner** bot               | yes     |
| VM3 | `10.0.3.53`  | Zoom **joiner** bot (3-party)     | yes     |
| VM5 | `10.0.4.16`  | Noise generator (`client/noise.py`) | native |

Noise also needs the **`iperf-server`** (`108.132.222.246`, outside the VPC).

> **Golden rule:** start the client **agents first** (they begin polling), **then** the
> orchestrator. An agent ignores any session that already existed when it started.

---

## Running unattended (bulk runs / when you log out)
The steps below run in the **foreground** — fine while you stay connected, but they **die the
moment you disconnect**. For anything long (a bulk run is hours), wrap each long-running command
in **`tmux`** so it keeps running after you log out:
```
tmux new -s bulk          # open a named session, then run your command inside it
#   detach, leaving it running:   Ctrl-b   then   d
tmux attach -t bulk       # reconnect any time to check on it   (tmux ls = list sessions)
```
Run one tmux session **on the VM the command lives on**: `noise` on VM5
(`python3 -m client.noise`), one `agent` each on VM1/VM2/VM3, and `bulk` on VM4
(`sudo -E python3 -m orchestrator.bulk_generate …`). To stop: attach, `Ctrl-C` — or
`tmux kill-session -t <name>`.

**Sturdier option for the agents** (also auto-restarts on crash/reboot): run the agent
container detached instead of in tmux — drop `--rm -it … bash`, add `-d --restart=always`, and
pass the command directly (one line; `--restart=always` can't combine with `--rm`):
```
docker run -d --restart=always --name zoom-agent --network host -v "$PWD":/tmp/py-zoom-meeting-sdk -w /tmp/py-zoom-meeting-sdk -e AGENT_IP=10.0.1.119 zoom-agent bash -lc "pip install boto3 zoom-meeting-sdk && python live_check_agent.py"
```
Stop these with `docker stop zoom-agent && docker rm zoom-agent`.

---

## 1. Sync code + clocks (every VM you're using)
The conversation-timing logic is split across machines (`turn_schedule.py` on VM4,
`bot.py` on the clients), so **all** must be current.
```
cd ~/zoom-meeting-orchestrator && git pull
chronyc tracking      # expect Reference ID 169.254.169.123, Leap: Normal, µs/ms offset
```
Run on VM4, VM1, VM2, VM3, and VM5 (with-noise).

## 2. Start noise — VM5 only, with-noise runs (`ssh 10.0.4.16`)
```
cd ~/zoom-meeting-orchestrator && python3 -m client.noise
```
Leave it running for the whole capture. Steady `start … / done …` lines = alive;
`download FAILED (moved 0 B …)` only if a host genuinely rejects us. Confirm listeners if
unsure: `ssh 108.132.222.246` → `systemctl is-active iperf3@5201 iperf3@5202 iperf3@5203`.

## 3. Set the roster — VM4, `live_check_orchestrator.py`
Edit so the `roster=[…]` lists exactly the VMs in this call. Exactly one `ROLE_HOST`;
`participant_count` is derived.
```python
roster=[
    RosterEntry(ip="10.0.1.119", zoom_role=ROLE_HOST),    # VM1
    RosterEntry(ip="10.0.2.67",  zoom_role=ROLE_JOINER),  # VM2
    RosterEntry(ip="10.0.3.53",  zoom_role=ROLE_JOINER),  # VM3 (3-party)
    RosterEntry(ip="10.0.4.16",  zoom_role=ROLE_NONE, noise=noise),  # VM5 (with-noise only)
]
```
- **With noise:** keep the VM5 line (`noise = SessionStore().read_noise_config().to_noise_block()`).
- **No noise:** ⚠️ **remove the VM5 line** — else the manifest claims noise the pcap won't contain.

## 4. Start the agents — each client VM (VM1, VM2, +VM3 for 3-party)
Enter the container (one line), then start the agent with **that VM's IP**:
```
docker run --rm -it --network host -v "$PWD":/tmp/py-zoom-meeting-sdk -w /tmp/py-zoom-meeting-sdk zoom-agent bash
pip install boto3 zoom-meeting-sdk
AGENT_IP=10.0.1.119 python live_check_agent.py    # VM1 (use .2.67 / .3.53 on VM2 / VM3)
```
Leave each sitting at `polling S3 for a new spec…` before moving on.

## 5. Run the orchestrator — VM4
```
sudo -E python3 live_check_orchestrator.py
```
`sudo` is required (tshark on `ens5`; pcap written under `/tmp`). It prints the
`session_id`, meeting id/passcode, and `preroll | duration | postroll`, then runs ~that
long and prints the manifest summary + S3 paths. **Note the meeting id + passcode** (or read
`aws s3 cp s3://zoom-bot-dataset-s3/sessions/<session_id>/spec.json -`).

## 6. Listen by ear — your laptop (the audio check packets can't prove)
Join the meeting in the Zoom client, **mic muted, camera off**, and listen:
- all bots **audible** and **alternating** (not just the host);
- the dynamics — a long pause, a brief talk-over, a short second-voice interjection.

(You join over the internet, not through VM4's NAT, so you never appear in the capture.
Ignore Zoom's "one active speaker" indicator — it's a known glitch.)

## 7. Verify the capture — VM4
```
aws s3 cp s3://zoom-bot-dataset-s3/sessions/<session_id>/manifest.json -
sudo /usr/bin/tshark -r /tmp/<session_id>.pcap -T fields -e ip.src | sort | uniq -c | sort -rn | head
```
Healthy: every rostered client IP has a real `t_join`/`t_leave` (not null); pcap shows each
client IP + Zoom relays as sources, **no `10.0.0.7`, no SSH**. With-noise: also `10.0.4.16`
+ `108.132.222.246` and web/video CDNs.

## 8. Label + QC — your laptop, from inside `py-zoom-meeting-sdk`
```
& "..\.venv\Scripts\python.exe" -m labeler.batch_label
```
Pulls all sessions, labels, pushes `labels.json`, prints OK/FLAG. For your session expect
**OK**, `warnings: []`, party count = roster, and (with-noise) plenty of `noise` flows with
both rules firing and zero leakage.

## 9. Stop
- Agents: `Ctrl-C`, then `exit` the container (`--rm` deletes it).
- Noise (VM5): `Ctrl-C`.
- VM4: `sudo rm -f /tmp/sess-*.pcap`; confirm `pgrep -a tshark dumpcap` prints nothing.
- Stop the VMs (and `iperf-server`) in the console when done.

> Leaving agents polling is fine for back-to-back runs — they pick up each new session.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: invalid reference format` | The `docker run` line got split — paste it as one line. |
| boto3 `Unable to locate credentials` | Missing `--network host` (needed to reach IMDS). |
| Agent never forks / matches no role | Wrong `AGENT_IP`, or that IP isn't in the VM4 roster. |
| `live_check_agent.py: No such file` | Untracked — `scp live_check_agent.py vm1:~/zoom-meeting-orchestrator/` from VM4. |
| `dumpcap … Permission denied` | pcap must be under `/tmp`; run the orchestrator with `sudo`. |
| Agent "won't stop" | It's a forever-poller — `Ctrl-C` it (the bot child already exited). |