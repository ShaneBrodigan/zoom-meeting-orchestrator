# Handoff Document — Zoom VoIP Dataset Generation Infrastructure
**Project:** MSc AI Thesis — Classifying Encrypted Zoom VoIP Traffic
**Student:** Shane Brodigan, x24309940, National College of Ireland
**Date:** 2026-06-02
**Current State:** Infrastructure complete and verified. VMs stopped to save cost. Refactor **in progress** — Phase 1 + Phase 2a–2f done; `common/s3.py` verified against real AWS on VM4 (2026-06-03, surfaced+fixed the IAM object-ARN bug in the IAM section below); `orchestrator/meeting_scheduler.py` (2e) live-verified on VM4 (2026-06-04). `orchestrator/capture.py` (2f) **live-verified on VM4 (2026-06-04)** — real tshark on `ens5` captured only pre-NAT client IPs; surfaced the dumpcap `/tmp` write-path requirement noted below.
**Next Session Focus:** Continue the refactor per `handovers/handoff_zoom_refactor_phase4.md` — 2f is now live-verified; next is `orchestrator/session_orchestrator.py` (2g), the first full orchestrated run. Remember 2g must write `capture.pcap` to a `/tmp` path (dumpcap privilege-drop note below).

For research scope, model rationale, evaluation methodology, and the topology diagram, see the methodology paper at `Shane_Brodigan_24309940__Practicum_Internship_Part_2.pdf`. **This document covers the deployed infrastructure only.** The bot programme design, S3 contract, orchestration flow, and the (now-resolved) open design questions live in [`REFACTOR_DESIGN.md`](./REFACTOR_DESIGN.md).

---

## Quick Reference

| VM | Role | Subnet | Private IP | Public Access |
|---|---|---|---|---|
| VM4 | Gateway / NAT / Capture host | public1 (10.0.0.0/24) | 10.0.0.7 | EIP `34.242.98.206` |
| VM1 | Zoom Bot Client A (host) | private1 (10.0.1.0/24) | 10.0.1.119 | via VM4 NAT |
| VM2 | Zoom Bot Client B (joiner) | private2 (10.0.2.0/24) | 10.0.2.67 | via VM4 NAT |
| VM3 | Zoom Bot Client C (joiner, 3-party only) | private3 (10.0.3.0/24) | 10.0.3.53 | via VM4 NAT |
| VM5 | Noise generator (iperf) | private4 (10.0.4.0/24) | 10.0.4.16 | via VM4 NAT |

All VMs: Ubuntu 24.04 LTS, t3.small, IAM role `zoom-bot-ec2-role` (S3 read/write on `zoom-bot-dataset-s3` only), key pair `zoom-capture-key`.

**Critical topology constraint:** Each client is in its own subnet by design. This forces Zoom to use its relay server architecture rather than P2P. If clients shared a subnet, Zoom would bypass the relay and the capture topology would break — VM4 (running tshark on `ens5`) would see no media traffic. Any refactor must preserve one-bot-per-subnet.

---

## VPC Architecture

### VPC
- **Name:** `zoom-capture` | **ID:** `vpc-0f9b79ce223bf681c` | **CIDR:** `10.0.0.0/16` | **Region:** eu-west-1
- **IGW:** `igw-02fe529378225db59` (attached) | DNS hostnames + resolution enabled

### Subnets

| Name | CIDR | Type | Route Table |
|---|---|---|---|
| zoom-capture-subnet-public1 | 10.0.0.0/24 | Public (via IGW) | Public |
| zoom-capture-subnet-private1 | 10.0.1.0/24 | Private | rtb-06e746feb02410489 |
| zoom-capture-subnet-private2 | 10.0.2.0/24 | Private | rtb-0f4bf0056fae6a80a |
| zoom-capture-subnet-private3 | 10.0.3.0/24 | Private | rtb-06e746feb02410489 |
| zoom-capture-subnet-private4 | 10.0.4.0/24 | Private | rtb-06e746feb02410489 |

All private-subnet route tables include `0.0.0.0/0 → eni-056a8c23b840089a5` (VM4's ENI) as their default route. This is why egress from private VMs flows through VM4 (where tshark observes it) rather than directly to the IGW.

### Security Groups

**`vm4-gateway-sg`** (VM4 only):
- *Inbound:* SSH (22) from `0.0.0.0/0` for EC2 Instance Connect; all traffic from `10.0.0.0/16`; SSH (22) from `18.202.216.48/29` (EC2 Instance Connect eu-west-1 range)
- *Outbound:* all traffic to `0.0.0.0/0` (AWS default)

**`client-sg`** (VM1, VM2, VM3, VM5) — `sg-0c407dad376da353a`:
- *Inbound:* all traffic from `10.0.0.0/16`
- *Outbound:* all traffic to `0.0.0.0/0`

> ⚠️ **Don't remove the `client-sg` outbound rule.** AWS SGs created via CLI/Terraform start with empty outbound, which silently denies all egress at the ENI. This rule is what allows packets from private VMs to reach VM4 for NAT. A previous debug session traced 4 hours of "NAT not working" to this missing rule.

### Network ACLs
All subnets use the default NACL (allow all in/out). Not blocking anything.

### VM4 — NAT Configuration

- **Source/dest check:** Disabled on `eni-056a8c23b840089a5` (required for an instance to forward traffic on behalf of others)
- **IP forwarding:** Persistent via `/etc/sysctl.d/99-nat.conf` containing `net.ipv4.ip_forward=1`
- **iptables MASQUERADE:** Persistent via `iptables-persistent` package, saved to `/etc/iptables/rules.v4`. Rule: `-t nat -A POSTROUTING -o ens5 -j MASQUERADE`
- **Persistence verified** via real stop/start cycle: NAT survives reboots without manual intervention

### VM4 — Capture Vantage Point (important for dataset correctness)

Because VM4 is a **single-ENI** NAT instance, `ens5` sees **two copies of every forwarded packet** — the pre-MASQUERADE copy and the post-MASQUERADE copy. Confirmed with an ICMP test (each ping appears four times on `ens5`):

```
10.0.1.119 > 8.8.8.8   echo request   ← PRE-NAT  (client's real IP)
10.0.0.7   > 8.8.8.8   echo request   ← POST-NAT (after MASQUERADE)
8.8.8.8 > 10.0.0.7     echo reply
8.8.8.8 > 10.0.1.119   echo reply
```

- **The EIP `34.242.98.206` never appears on `ens5`.** MASQUERADE rewrites the source to VM4's *private* IP `10.0.0.7`; the private↔EIP swap happens later at the **Internet Gateway**, beyond VM4. So the post-NAT copy on `ens5` shows `10.0.0.7`, not the EIP.
- **For the dataset we capture the PRE-NAT copy** (real client IPs → per-participant attribution; emulates an in-LAN monitor before the edge NAT). The post-NAT copy collapses all clients to `10.0.0.7` and is discarded. Capture filter that selects exactly one clean pre-NAT copy per direction and drops the `10.0.0.7` twins + SSH:
  ```
  tshark -i ens5 -f '(net 10.0.1.0/24 or net 10.0.2.0/24 or net 10.0.3.0/24 or net 10.0.4.0/24) and not tcp port 22'
  ```
  This keeps Zoom media (real client src IPs) **and** VM5 iperf noise (`10.0.4.16`); excludes VM4's own control traffic.
- **S3 control traffic is invisible to this capture.** Client↔S3 rides the **VPC Gateway Endpoint** (routing-layer; more specific than `0.0.0.0/0 → VM4`), so it never hairpins through VM4 and never reaches `ens5`. This is *why* the refactor uses S3 — not SSH — as the per-session control plane: orchestration cannot leak into the labeled PCAPs. (SSH, by contrast, *is* visible on `ens5`; hence SSH is restricted to provisioning outside capture windows.)
- **Clock sync prerequisite:** dataset labels join the PCAP (VM4 clock) to bot heartbeats (client clocks) by timestamp, so all 5 VMs must run **chrony against the AWS Time Sync Service (`169.254.169.123`)**. Sub-ms in-VPC; verify enabled when VMs are running.

### S3
- **Bucket:** `zoom-bot-dataset-s3` (eu-west-1)
- **Folders:** `input_audio/` (contains `librispeech_audio.pcm`, a 1GB trimmed subset of LibriSpeech train-clean-100), `sessions/` (populated at runtime, one folder per call session)
- **VPC Gateway Endpoint:** Configured so EC2 → S3 traffic stays on AWS internal network (free, no NAT bandwidth used)

### IAM
- **Policy:** `zoom-bot-s3-policy` — `s3:ListBucket` on `arn:aws:s3:::zoom-bot-dataset-s3` (the bucket ARN) and `s3:GetObject`/`s3:PutObject` on `arn:aws:s3:::zoom-bot-dataset-s3/*` (the object ARN). **No `s3:DeleteObject`** — a bot deliberately cannot delete dataset objects.
- **Role:** `zoom-bot-ec2-role` — attached to all 5 VMs
- **Scope is intentionally narrow.** No `ec2:Describe*` perms. From inside a VM you cannot list other instances, IPs, or VPC config via AWS CLI — use the AWS Console or your local AWS CLI for that.
- ⚠️ **The object actions need the `/*` ARN.** Originally the policy listed `GetObject`/`PutObject` against the *bucket* ARN (and a mistyped `zoom-bot-dataset/*` missing the `-s3`), so `ListBucket` worked but every object read/write returned `AccessDenied`. Fixed 2026-06-03 after the `common/s3.py` smoke test surfaced it. `ListBucket` belongs on the bucket ARN; `GetObject`/`PutObject` belong on the `/*` object ARN — don't collapse them onto one resource.

---

## SSH Topology

```
Local laptop ──(EIP)──> VM4 ──(private subnets via VPC)──> VM1, VM2, VM3, VM5
```

VM4 acts as bastion. The key (`zoom-capture-key.pem`, chmod 400) lives at `~/.ssh/zoom-capture-key.pem` on VM4. `~/.ssh/config` on VM4 has aliases `vm1`, `vm2`, `vm3`, `vm5` mapping to the private IPs above, so `ssh vm1` from VM4 just works.

The client VMs cannot be SSH'd into directly from the internet — they have no public IPs and are in private subnets. Always go through VM4.

---

## What's Installed Where

| VM | Software |
|---|---|
| VM4 | AWS CLI v2, tshark (capture-only, non-root capture disabled — use `sudo`), iptables-persistent |

> ⚠️ **tshark/dumpcap capture-file path on VM4.** `dumpcap` (tshark's write helper) **drops privileges
> before opening the `-w` output file**, so even under `sudo` it cannot write into a home dir (mode 755)
> — it prints `Capturing on 'ens5'` then fails with `could not be opened: Permission denied`. It writes
> fine to **`/tmp`** (mode 1777). Not AppArmor (`dmesg | grep -i denied` empty). **Always write session
> pcaps to a `/tmp` path** (a root-created `/tmp` subdir is 755 and re-breaks it — capture directly into
> `/tmp` or make the subdir `1777`). Confirmed 2026-06-04 live-verifying `orchestrator/capture.py`.
| VM1 | Docker CE + Compose plugin (official Docker apt repo, not snap/docker.io). User `ubuntu` is in `docker` group. |
| VM2 | Same as VM1 |
| VM3 | Docker CE + Compose plugin (official Docker apt repo). Provisioned identically to VM1/VM2 — ready as 3-party joiner. |
| VM5 | Noise node. Docker + iperf install pending; additional ENIs not yet attached. Sole iperf noise generator for now (more noise sources may be added later, incl. concurrent iperf on VM1/2/3). |

The official Docker repo install is intentional. Ubuntu's `docker.io` package lags behind, and the snap version has known container networking issues that affect bots needing to bind to ALSA/Pulse.

---

## Verified Behaviour (don't re-test unless something changes)

- VM4 internet connectivity (0% loss, ~0.9ms to 8.8.8.8)
- VM4 → S3 (role + bucket access confirmed)
- VM4 → all 4 private VMs (ping, SSH)
- All 4 private VMs → internet via VM4 NAT (tcpdump on VM4 `ens5` shows both pre- and post-MASQUERADE packets, TTL decrements 64→63 outbound confirming L3 hop)
- NAT config survives VM4 stop/start cycle
- Docker `hello-world` succeeds on VM1, VM2, and VM3 (confirms NAT-mediated pulls from Docker Hub work; VM3 provisioned identically to VM1/VM2)
- tshark captures cleanly on `ens5` with both raw `-i ens5` and BPF filters

---

## Open Tasks (post-refactor)

1. Build/pull the `py-zoom-meeting-sdk` image on VM1, VM2, VM3 (image structure now settled in `REFACTOR_DESIGN.md` §9: single container polls S3, forks a child per session)
2. Install Docker + iperf on **VM5** (VM3 is already Docker-provisioned)
3. Attach additional ENIs to VM5 for multi-IP noise generation (noise should appear to come from multiple distinct source IPs to be realistic)
4. First end-to-end 2-party call: VM1 host, VM2 joiner, tshark capturing on VM4, session manifest written to S3
5. Scale to 3-party (VM3 joins), then add VM5 noise at varying intensities; later, concurrent iperf on the VoIP VMs (real mixed traffic, not synthesized)
6. Confirm **chrony / AWS Time Sync** is enabled on all 5 VMs (label timestamp alignment — see Capture Vantage Point above)
7. **Cleanup:** Delete the VPC Flow Log + CloudWatch log group `vpc-flow-logs-debug` (created during NAT debug, never deleted — small ongoing CloudWatch costs)

---

## Open Design Questions for Code Refactor — RESOLVED

> **These questions are now resolved.** Full decisions + rationale are in
> [`REFACTOR_DESIGN.md`](./REFACTOR_DESIGN.md) §2. Infra-relevant outcomes summarised here; the
> trade-off discussion below is retained for context.

**Infra-relevant resolutions (the rest are bot-programme detail in `REFACTOR_DESIGN.md`):**
- **Orchestration:** VM4 orchestrates; per-session control plane is **S3 (gateway endpoint, invisible to capture)**, not SSH. SSH only for provisioning.
- **Capture lifecycle:** VM4 starts tshark **before** publishing the S3 spec (guarantees full capture), ends the meeting via REST at expiry (hard media stop), stops tshark after a random post-roll. One `capture.pcap` per session.
- **Manifest IP / `participant_ips`:** **client private IPs** (pre-NAT capture — see Capture Vantage Point above), e.g. `10.0.1.119`, `10.0.2.67`, `10.0.3.53`, noise `10.0.4.16`.
- **Role assignment:** each client self-identifies by its **private IP** against the spec roster (network position = single source of truth).
- **Noise:** orthogonal to role (`zoom_role` + independent `noise` block); VM5-only now, real concurrent capture later (never IP-rewriting).

The current `sample_program/` code structure was designed for single-machine demo runs. Moving to the distributed topology raised these orchestration questions; they are recorded below as originally posed, now answered above / in `REFACTOR_DESIGN.md`.

### Orchestration ownership
Who decides what a session looks like (participant count, noise level, audio sample, duration) and distributes that config? Options:
- **VM4 as orchestrator.** Schedules meetings via Zoom REST API, generates session config, pushes credentials/config to clients (via SSH? S3 manifest poll? other?). Pros: single source of truth, easier to label captures. Cons: tight coupling between orchestration and capture host.
- **Each client self-configures from S3.** VM4 just writes a session spec to S3, clients poll. Pros: looser coupling. Cons: race conditions on session start, harder to handle partial failures.

### Join synchronisation
If VM1 joins 8 seconds before VM2, the "2-party" ground-truth label is briefly wrong (it's really a 1-party call until VM2 arrives). Does the capture start at "all expected participants joined" or at "first participant joined"? How is the join barrier implemented across VMs?

### Bot role assignment
How does a bot know whether it's the host, joiner-2, joiner-3, or noise generator? Options: env var at container launch, derive from VM private IP (since each subnet = one role), hardcode at image build, fetch from S3 session spec. The handoff's existing `BOT_ROLE` env var pattern suggests env-var-at-launch was the intent, but check whether `CallSpawner.py` actually supports being invoked with different roles.

### Session manifest authorship
The manifest (per-session ground truth: participants, IPs, roles, noise level, turn schedule, start/end times) needs to be definitive. Options:
- Each bot writes its slice to `sessions/{id}/{role}.json`, VM4 merges post-call
- VM4 writes the full manifest before the call starts, bots just consume
- VM4 writes a partial manifest pre-call, appends actual timings post-call

The third is probably most robust but most code. Pick deliberately.

### Capture lifecycle
tshark on VM4 needs to start before any bot traffic and stop after all bots disconnect. Options:
- VM4 orchestrator starts tshark, dispatches bot start commands, waits for all-disconnected signal, stops tshark
- tshark runs continuously, captures are sliced post-hoc by timestamp from manifests
- Each bot signals start/stop to VM4 which manages tshark accordingly

Continuous capture is operationally simplest but produces giant PCAPs; per-session start/stop is more code but cleaner downstream.

### Audio playback realism
The methodology paper notes that continuous LibriSpeech playback produces uniform packet timing unrepresentative of real VoIP. Each bot needs randomised pause insertion. Refactor question: is pause logic in the bot itself, or pre-baked into per-session audio files (less code, less flexibility)?

### Feature/label coupling at write-time
The downstream models (RF, GRU, ET-BERT) need flow-level features keyed by 5-tuple. The capture (PCAP) and the manifest (ground truth) are joined by timestamp + IP pair. If the bot reports its public-facing IP wrong (e.g., reports VM1's private IP when from Zoom's perspective the source was VM4's EIP after NAT), the join breaks. Confirm during refactor: what IP should appear in `participant_ips` in the manifest — the bot's private IP, VM4's EIP, or the IP Zoom sees the bot from?

---

## Cost Posture

- **Running (all 5 VMs):** ~$85/month (compute) + ~$5/month storage + ~$3.60/month EIP = ~$93/month
- **Stopped (current state):** ~$5/month EBS + ~$3.60/month EIP + ~$0.02/month S3 = ~$9/month
- **Variable:** S3 PUT/GET fees during dataset generation (minor at this scale)

AWS now charges for public IPv4 addresses (~$3.60/month per EIP) regardless of whether the associated instance is running, since Feb 2024. Releasing the EIP would save this but means re-associating a new EIP next time, with a different IP — which would also require updating any local SSH configs and Zoom OAuth redirect URLs if applicable.

---

## Local Bot Code Structure

> ⚠️ **Correction:** an earlier version of this section described a `BOT_ROLE` env var, a
> `CallSpawner(participant_count, noise_level, is_group_call)` signature, and
> `PARTICIPANT_COUNT` / `NOISE_LEVEL` / `IS_GROUP_CALL` / `AUDIO_BUCKET` env vars. **None of these
> exist in the actual code** — they were aspirational. The real pre-refactor state and the target
> structure are documented in [`REFACTOR_DESIGN.md`](./REFACTOR_DESIGN.md) §1 and §5.

**Actual pre-refactor code (for reference):**
```
py-zoom-meeting-sdk/
├── Dockerfile                ← ubuntu:22.04 base, ALSA, PulseAudio, Zoom SDK deps
├── compose.yaml              ← mounts host dir into /tmp/py-zoom-meeting-sdk
└── sample_program/
    ├── main.py               ← calls CallSpawner(2, 1, True) — no role concept
    ├── CallSpawner.py        ← (num_of_bots, meeting_dur_in_mins, has_screenshare); creates meeting
    │                            AND forks all bots locally via multiprocessing — single-machine demo
    ├── MeetingScheduler.py   ← Zoom REST API / S2S OAuth (keep)
    ├── MeetingJoiner.py      ← GLib loop wrapper around the bot
    ├── meeting_bot.py        ← heavy bot: audio+video+screenshare+Deepgram (to be stripped)
    └── input_audio/          ← local audio (do not bake into Docker image — fetch from S3)
```

**Environment variables the current code actually reads:**
```
ZOOM_S2S_ACCOUNT_ID        # meeting scheduler (REST/S2S OAuth)
ZOOM_S2S_CLIENT_ID
ZOOM_S2S_CLIENT_SECRET
ZOOM_APP_CLIENT_ID         # SDK JWT auth (in meeting_bot.py)
ZOOM_APP_CLIENT_SECRET
RECORD_VIDEO               # optional; removed in refactor
DEEPGRAM_API_KEY           # optional; removed in refactor
```
The post-refactor runtime config moves to the **S3 spec contract** (`REFACTOR_DESIGN.md` §3), not env
vars. Audio bucket is `zoom-bot-dataset-s3` (see S3 section above). Code is in a git repo.

---

## Suggested Skills

- `handoff` — to refresh this infra doc (or `REFACTOR_DESIGN.md`) as decisions/infra evolve
- `verify` / `run` — to validate the first end-to-end 2-party call against real behaviour (downlink audio, distinct-participant joins, capture-filter correctness) rather than assuming
- `code-review` — on the refactor diff before generating any dataset; silent capture/label bugs corrupt the dataset invisibly
- `grill-me` — only if a *new* design fork appears (already used to produce `REFACTOR_DESIGN.md`)
