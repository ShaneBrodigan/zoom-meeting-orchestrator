/co# Handoff Document — Zoom VoIP Dataset Generation Infrastructure
**Project:** MSc AI Thesis — Classifying Encrypted Zoom VoIP Traffic
**Student:** Shane Brodigan, x24309940, National College of Ireland
**Date:** 2026-06-02
**Current State:** Infrastructure complete and verified. Refactor **in progress** — Phase 1 + Phase 2a–2g done; `common/s3.py` verified against real AWS on VM4 (2026-06-03, surfaced+fixed the IAM object-ARN bug in the IAM section below); `orchestrator/meeting_scheduler.py` (2e) live-verified on VM4 (2026-06-04). `orchestrator/capture.py` (2f) **live-verified on VM4 (2026-06-04)** — real tshark on `ens5` captured only pre-NAT client IPs; surfaced the dumpcap `/tmp` write-path requirement noted below. `orchestrator/session_orchestrator.py` (2g), the VM4 conductor, **built + unit-verified + live-verified on VM4 2026-06-08** (live plumbing run — 0 packets as expected with the client VMs down). **Phase 3 (the `client/` package) built + unit-verified, and the first end-to-end 2-party no-noise call LIVE-VERIFIED 2026-06-09** (session `sess-20260609T150600Z-9bda`: VM1 host + VM2 joiner joined as distinct participants, real `joins_leaves` in the manifest, 28 MB / 24,371-packet pcap with pre-NAT client IPs `10.0.1.119`/`10.0.2.67` + Zoom relay downlink; VM3 idle negative control; capture filter clean — no `10.0.0.7`/SSH). **3-party LIVE-VERIFIED 2026-06-10** (session `sess-20260610T105421Z-0d1f`: VM1 host + VM2 + VM3 joiners, three populated `joins_leaves` with a real ~39 s three-party overlap, all three left within 0.02 s on the REST hard-stop; pcap 29,485 packets, all three pre-NAT client IPs present, no `10.0.0.7` twins/SSH). **§7 step 5 (VM5 iperf noise) design converged via grill-me 2026-06-10, then BUILT + unit-verified + `/code-review`'d 2026-06-10** (`common/noise_config.py` + `common/s3.py` `read_noise_config()` + standalone `client/noise.py`; 121 tests pass, all local — no AWS yet). Live noise validation deferred to a single combined run *with* the labeler — see `handovers/handoff_zoom_refactor_phase9.md` + `REFACTOR_DESIGN.md` decision 10 / §7 step 5. VMs normally stopped to save cost.
**Next Session Focus:** Continue the refactor per the newest file in `handovers/` (`handoff_zoom_refactor_phase11.md`). The offline labeler is built AND live-validated on a fresh 3-party capture (`sess-20260612T103058Z-4e80`, 2026-06-12). The **one remaining step is the VM5 noise live test** — ONE combined live AWS run (3-party call + VM5 iperf noise together) that validates noise capture + the labeler's noise separation on real mixed traffic in a single VM spin-up. Infra prereqs (iperf server outside the VPC, `config/noise.json`, Docker+iperf3 on VM5) are in Open Tasks below + the phase-11 handoff. Live-noise infra prereqs still open (see Open Tasks): the **dedicated internet iperf server outside the VPC** (Shane standing up a new instance; SG open TCP+UDP from VM4 EIP `34.242.98.206`), Docker + iperf3 on VM5, and `config/noise.json` in S3 (not created yet — Claude can draft a starter). Live-run infra prereqs: Docker + iperf3 on VM5, the internet iperf server (SG open TCP+UDP), and `config/noise.json` in S3 (see Open Tasks). See **`launch-guide.md`** (repo root) for the step-by-step run procedure. NOTE: the repo is cloned to `~/zoom-meeting-orchestrator` on **all** VMs (VM4 runs the orchestrator natively; VM1/VM2/VM3 run the agent inside the SDK Docker image with `--network host`). `.env` is gitignored so a fresh clone lacks it — recreate it: VM4 needs `ZOOM_S2S_*`, the client VMs need `ZOOM_APP_CLIENT_ID`/`ZOOM_APP_CLIENT_SECRET`. VM4 deps: boto3+requests already in system python3, `sudo apt-get install -y python3-dotenv` (pip absent). The client container needs `pip install boto3 zoom-meeting-sdk` (not baked into the image).

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
- **Folders:** `input_audio/` (contains `librispeech_audio.pcm`, a 1GB trimmed subset of LibriSpeech train-clean-100), `sessions/` (populated at runtime, one folder per call session), `config/noise.json` (single source of truth for VM5 noise — iperf endpoint/ports + ranges + seed; VM5 reads it to run, VM4 reads it to record into the spec/manifest. **Created 2026-06-12**: `target=108.132.222.246` + ports `5201,5202,5203` TCP/UDP, rate `1.0–50.0` Mbps; see `REFACTOR_DESIGN.md` decision 10)
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

VM4 acts as bastion. The key (`zoom-capture-key.pem`, chmod 400) lives at `~/.ssh/zoom-capture-key.pem` on VM4. `~/.ssh/config` on VM4 has aliases `vm1`, `vm2`, `vm3` mapping to the private IPs above, so `ssh vm1` from VM4 just works. **`vm5` may NOT be aliased** — in the 2026-06-12 noise run `ssh vm5` did not resolve to VM5; reach it directly with **`ssh 10.0.4.16`** from VM4 (or add the alias). ⚠️ The hosts look alike — confirm the prompt (`ip-10-0-4-16` = VM5) before running `client/noise.py`; it failed this session from being launched on the wrong box (the iperf-server has no IAM role; VM3 has no iperf3).

The client VMs cannot be SSH'd into directly from the internet — they have no public IPs and are in private subnets. Always go through VM4. **Exception:** the separate `iperf-server` (default VPC, EIP `108.132.222.246`) is **not** behind VM4 — SSH it directly from the laptop with the same `zoom-capture-key`.

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
| VM1 | Docker CE + Compose plugin (official Docker apt repo, not snap/docker.io). User `ubuntu` is in `docker` group. Repo cloned to `~/zoom-meeting-orchestrator`; `zoom-agent` SDK image built (2026-06-09); ran the host bot. |
| VM2 | Same as VM1; ran the joiner bot (2026-06-09). |
| VM3 | Same as VM1; `zoom-agent` image built, agent run as a negative control 2026-06-09 (idle — not in the 2-party roster). Ready as 3-party joiner. |
| VM5 | Noise node — runs the **standalone** `client/noise.py` (iperf *client*) independently of any session; not in the agent poll loop. **Provisioned natively 2026-06-12** (no Docker): `iperf3` + `python3-boto3` installed, repo cloned to `~/zoom-meeting-orchestrator`, S3 read via instance role confirmed, chrony locked to `169.254.169.123` (~4 µs offset). Additional ENIs not yet attached. The iperf *server* is the **separate `iperf-server` host outside the VPC** at EIP `108.132.222.246` (see below; not VM5). Sole iperf noise generator for now (more noise sources may be added later, incl. concurrent iperf on VM1/2/3). |
| **iperf-server** | **Noise-target host, OUTSIDE the capture VPC** (in the **default VPC**, eu-west-1, launched 2026-06-12). t3.micro, Ubuntu 24.04 LTS, key pair `zoom-capture-key`, **no IAM instance profile** (it never touches S3). **Elastic IP `108.132.222.246`** — stable across stop/start, and it is the `target` value in `config/noise.json`. SG `launch-wizard-1`: *inbound* SSH 22 from `0.0.0.0/0` + TCP & UDP 5201–5203 from VM4's EIP `34.242.98.206/32` (the post-NAT source of VM5's noise); *outbound* all traffic. `iperf3` installed; **3 persistent listeners running** (`iperf3@5201/5202/5203` systemd template, `enabled` so they survive reboot/stop-start) — DONE 2026-06-12. This host is the labeler's noise anchor (`src=10.0.4.16 & dst=108.132.222.246` → noise). |

> The client container runs with `--network host` (needed so boto3 reaches the instance-role creds via
> IMDS, and so the agent auto-detects the VM's real private IP). `boto3` + `zoom-meeting-sdk` are
> `pip install`ed inside the container per run — they are **not** baked into the image. See
> `launch-guide.md` for the full client launch procedure.

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
- **chrony / AWS Time Sync confirmed on all 5 VMs** — VM1–VM4 (2026-06-09) and **VM5 (2026-06-12)**;
  `chronyc tracking` showed `169.254.169.123`, sub-ms offsets (VM5 ~4 µs).
- **First end-to-end 2-party call captured (2026-06-09, `sess-20260609T150600Z-9bda`):** VM1 host + VM2 joiner joined as distinct participants; the tshark capture filter grabbed real pre-NAT client media (`10.0.1.119`, `10.0.2.67`) + Zoom relay downlink, 28 MB / 24,371 packets, no `10.0.0.7` twins or SSH; manifest `joins_leaves` populated. Confirms the whole pre-NAT capture + per-participant attribution chain end to end.
- **3-party call captured (2026-06-10, `sess-20260610T105421Z-0d1f`):** VM1 host + VM2 + VM3 joiners, three populated `joins_leaves` (real ~39 s three-party overlap window; all three left within 0.02 s on VM4's REST meeting-end); pcap 29,485 packets with all three pre-NAT client IPs (`10.0.1.119` 65.95%, `10.0.3.53` 17.83%, `10.0.2.67` 16.22%) outbound + Zoom relay (`170.114.45.1`, `144.195.37.123`) downlink, no `10.0.0.7` twins or SSH. Step 4 done — code unchanged from 2-party (one-line roster edit only).
- **3-party call re-run + labeler live-validated (2026-06-12, `sess-20260612T103058Z-4e80`):** clean
  3-party capture (code unchanged), then `labeler/derive_labels.py` run **on VM4** over the
  downloaded manifest+pcap. Timeline ramped `0→1→2→3→2→1→0` (~66 s three-party window), no warnings;
  flows `zoom_signaling: 109, zoom_media: 39, other: 15`. Per-label remote-endpoint audit confirmed
  the Zoom labels held only Zoom ranges (`170.114.*`, `144.195.*`, `206.247.*`, `134.224.*`) + Zoom's
  CDN (`52.84.151.*`) + one Zoom-on-AWS IP, while host/NTP housekeeping (Canonical `185.125.190.*` /
  `91.189.91.157`, Cloudflare NTP `162.159.200.1`, HEAnet mirror `193.1.208.194`) correctly landed in
  `other` — NOT mislabeled as Zoom. Labeler validated on real traffic; no rule change needed.
  (Tooling note: VM4 ran the labeler after `git pull` + `sudo apt-get install -y python3-scapy` —
  pip is absent on VM4, so scapy comes from apt.)
- **3-party call + VM5 iperf noise, end-to-end (2026-06-12, `sess-20260612T135535Z-23fa`):** the combined
  run that closed §7 step 5. `client/noise.py` ran live on VM5 (seeded TCP/UDP bursts to `iperf-server`
  `108.132.222.246`), confirmed flowing **pre-NAT** on `ens5` as `10.0.4.16 → 108.132.222.246`. 3-party
  Zoom call captured alongside it (real `joins_leaves`, ~68 s three-party window). pcap clean under heavy
  noise — sources were the 3 client IPs + Zoom relays + `10.0.4.16` (33k) + `108.132.222.246` (110k), **no
  `10.0.0.7` twins / no SSH** (noise was ~80 % of packets, a tunable `rate_mbps` choice). Manifest had a
  4-entry roster with VM5's populated noise block + 3 `joins_leaves` (VM5 never joins). Labeler (on VM4):
  timeline `0→1→2→3→2→1→0`, flows `noise:24, zoom_media:39, zoom_signaling:107, other:35`; **all 24 noise
  flows exactly `10.0.4.16 ↔ 108.132.222.246`, rule `noise-vm-to-iperf-server`, zero leakage, no warnings.**
  Harness feature-complete for the `audio` profile.

---

## Open Tasks (post-refactor)

1. ~~Build the `py-zoom-meeting-sdk` image on VM1, VM2, VM3~~ **DONE 2026-06-09** (`zoom-agent` image built on all three; the container runs the agent which forks a bot child per session).
2. ~~**VM5 noise live-run prerequisites**~~ **ALL DONE 2026-06-12** — combined run
   `sess-20260612T135535Z-23fa` succeeded; harness feature-complete for the `audio` profile (see
   `handovers/handoff_zoom_refactor_phase12.md`). Prereq detail kept below for reference:
   (design converged 2026-06-10 — `REFACTOR_DESIGN.md` decision 10):
   - ~~Install **Docker + iperf3 on VM5**~~ **DONE 2026-06-12 — provisioned NATIVELY, no Docker.** Noise
     doesn't use the Zoom SDK, so VM5 just needs `iperf3` + `python3-boto3` + the repo cloned to
     `~/zoom-meeting-orchestrator`; running `python3 -m client.noise` natively gives it the real
     `10.0.4.16` source IP and instance-role S3 access for free. Verified: `SessionStore().read_noise_config()`
     read `config/noise.json` from S3 (target `108.132.222.246`) using the instance role.
   - ~~Stand up the **dedicated internet iperf server** (a tiny host *outside* the VPC)~~ **DONE
     2026-06-12** — `iperf-server` t3.micro in the **default VPC**, Elastic IP **`108.132.222.246`**, key
     `zoom-capture-key`, no IAM role; SG `launch-wizard-1` opens TCP **and** UDP 5201–5203 from VM4's EIP
     `34.242.98.206/32` (+ SSH 22). `iperf3` installed; 3 persistent listeners running via systemd template
     `iperf3@5201/5202/5203` (`enabled`, one per port — `noise.py` runs one burst at a time, no concurrency).
   - ~~Create **`config/noise.json`** in `s3://zoom-bot-dataset-s3/`~~ **DONE 2026-06-12** — uploaded with
     `target=108.132.222.246`, `ports=[5201,5202,5203]`, `protocols=[tcp,udp]`, `rate_mbps=[1.0,50.0]`,
     `burst_s=[2,10]`, `gap_s=[0.5,5.0]`, `reverse_prob=0.5`, `seed=4711`; validated on VM4 via
     `SessionStore().read_noise_config().to_noise_block()` (loads clean, passes the rate-floor guard).
3. Attach additional ENIs to VM5 for multi-IP noise generation (noise should appear to come from multiple distinct source IPs to be realistic) — *deferred; the noise build targets a single source IP now, structured so this is a later flip.*
4. ~~First end-to-end 2-party call: VM1 host, VM2 joiner, tshark on VM4, manifest to S3~~ **DONE 2026-06-09** (`sess-20260609T150600Z-9bda`; see Verified Behaviour).
5. ~~Scale to 3-party~~ **DONE 2026-06-10** (`sess-20260610T105421Z-0d1f`). ~~add VM5 noise~~ **DONE
   2026-06-12** (`sess-20260612T135535Z-23fa`; 3-party + VM5 iperf noise, labeler separated noise cleanly).
   **Next (future, §7 step 7):** concurrent iperf on the VoIP VMs (real mixed traffic, not synthesized);
   multi-ENI source IPs on VM5; `media_profile: audiovideo` class.
6. ~~Confirm **chrony / AWS Time Sync**~~ confirmed on VM1–VM4 (2026-06-09) **and VM5 (2026-06-12** —
   `chronyc tracking` → `169.254.169.123`, ~4 µs offset). All 5 VMs confirmed.
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
- **`iperf-server` (added 2026-06-12):** ~$7.50/month compute when running (t3.micro) + a second EIP at
  ~$3.60/month. The EIP is billed even while the instance is **stopped** (AWS charges for an EIP attached
  to a stopped instance), so stop *and* release it if the noise work is done for a while — but releasing
  means a new IP, which would need `config/noise.json` + this doc updated. Negligible data-transfer-out
  during a short test run.
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
