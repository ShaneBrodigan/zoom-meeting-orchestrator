# REFACTOR_DESIGN — Distributed Zoom VoIP Dataset Harness

**Project:** MSc AI Thesis — Classifying Encrypted Zoom VoIP Traffic
**Student:** Shane Brodigan, x24309940, National College of Ireland
**Status:** Design converged (grill-me session, 2026-06-02). Implementation in progress — Phase 1 + Phase 2a–2d done and `common/s3.py` verified against real AWS (2026-06-03). See `handovers/handoff_zoom_refactor_phase3.md` for current progress.
**Companion docs:** infrastructure in [`handoff_zoom_aws_setup.md`](./handoff_zoom_aws_setup.md); research scope/methodology in `Shane_Brodigan_24309940__Practicum_Internship_Part_2.pdf`.

This document records the design for refactoring `sample_program/` from a single-machine
`multiprocessing` demo into a distributed, one-bot-per-subnet harness that generates **labeled
encrypted Zoom VoIP captures** across the AWS VPC in the infrastructure handoff. It captures the
*decisions and their rationale* so implementation can proceed without re-litigating them. It does
not duplicate the VPC/IAM/SSH facts in `handoff_zoom_aws_setup.md` — see that doc for IPs, subnets,
security groups, S3 bucket, and NAT details.

---

## 1. Problem with the current code (pre-refactor reality)

The handoff doc's description of the code was **aspirational**; the real code differs:

- `main.py` simply calls `CallSpawner(2, 1, True)`. There is **no role concept**, no `BOT_ROLE`,
  and none of the `PARTICIPANT_COUNT` / `NOISE_LEVEL` / `IS_GROUP_CALL` / `AUDIO_BUCKET` env vars
  the handoff listed.
- `CallSpawner.__init__(num_of_bots, meeting_dur_in_mins, has_screenshare)` — **not**
  `(participant_count, noise_level, is_group_call)`. So `CallSpawner(2, 1, True)` = 2 bots, 1-minute
  meeting, screenshare on.
- **Everything runs on one machine.** `CallSpawner` creates the meeting via REST *and* forks all N
  bots locally via `multiprocessing.Process`. This is the opposite of the required topology — all
  bots sharing one host would let Zoom go P2P and break the capture vantage point.
- The bot (`meeting_bot.py`, ~670 lines) records received audio, renders incoming video to PNGs,
  runs a virtual camera, screen-shares frames, transcribes via Deepgram, sends chat, handles
  breakout rooms. Most of this **pollutes** an audio-VoIP capture.
- Audio is hardcoded to `sample_program/input_audio/librispeech_audio.pcm`, not fetched from S3.

The refactor is therefore architectural, not a config tweak.

---

## 2. Converged decisions (with rationale)

1. **Orchestration ownership — VM4 orchestrates; control plane is S3, not SSH.**
   VM4 is the only node that reaches every subnet. *Critical constraint discovered:* VM4 is also
   the tshark capture host, so any control traffic it exchanges with clients risks being captured
   and learned by the model as a non-representative artifact. **SSH control would be visible on
   `ens5`; S3 control is not** — the S3 VPC Gateway Endpoint is implemented in the VPC routing layer,
   so client↔S3 traffic never hairpins through VM4. Therefore: per-session control rides S3; clients
   run a poller; **SSH is used only for cold-start provisioning, outside capture windows.**

2. **Capture point & participant IP identity — pre-NAT on `ens5`, client private IPs.**
   VM4 is a single-ENI NAT instance; `ens5` sees *both* pre- and post-MASQUERADE copies of every
   packet (confirmed by an ICMP `tcpdump`: each ping appears as `10.0.1.119→8.8.8.8` *and*
   `10.0.0.7→8.8.8.8`). The post-NAT copy collapses all clients to VM4's IP (on `ens5` this is the
   private `10.0.0.7`; the EIP swap happens later at the IGW and is never visible on `ens5`).
   Per-participant attribution — central to the model's goal of "which IPs are on a call" — requires
   the **pre-NAT** copy. This also emulates the realistic vantage point: a monitor *inside* the LAN,
   before the edge NAT.
   **Capture filter (selects exactly one clean pre-NAT copy per direction, drops the `10.0.0.7`
   twins, excludes SSH):**
   ```
   tshark -i ens5 -f '(net 10.0.1.0/24 or net 10.0.2.0/24 or net 10.0.3.0/24 or net 10.0.4.0/24) and not tcp port 22'
   ```
   `participant_ips` in the manifest = client private IPs (VM1 `10.0.1.119`, VM2 `10.0.2.67`,
   VM3 `10.0.3.53`, VM5 noise `10.0.4.16`). VM5 iperf noise is included automatically.

3. **Role assignment — self-identify by IP from the spec roster.**
   Role is physically welded to subnet (VM1 is host *because* it is in private1, which is what the
   capture labels on). To make label-vs-subnet drift *impossible*, VM4 writes a roster (role↔IP)
   into the spec and each client reads its own private IP and looks itself up. Network position is
   the single source of truth — no per-VM role config to misconfigure.

4. **Join synchronization & labeling — randomized timing + heartbeats + timeline labels.**
   A flat "N-party" label is wrong during the join ramp (if VM1 joins 8 s before VM2, that window is
   really 1-party). **Randomized** join offsets/pre-roll/post-roll are deliberately used to avoid the
   model learning the harness's fixed timing as a fingerprint (which would not generalize to real
   calls). Bots **heartbeat actual join/leave timestamps to S3**; labels are a **timeline**
   (`[t,·)=1-party`, `[·,·)=2-party …`) derived offline. Random *timing*, recorded *truth*.

5. **Capture lifecycle — tshark-before-spec; REST meeting-end is the hard stop.**
   The one ordering invariant: **tshark must already be running before any bot can join.** Achieved
   by sequencing — VM4 starts tshark *before* publishing the spec (clients cannot see the session
   until the spec exists), so every join is captured while pre-roll length and join offsets stay
   random. VM4 ends the meeting via REST at expiry, which stops all media *hard* regardless of bot
   health, so the stop trigger is simply `meeting_end + random post-roll` (no event-stop/timeout
   machinery needed). One `capture.pcap` per session.

6. **Manifest authorship — VM4 merges raw facts; labels derived offline.**
   Two information kinds must not be conflated: *raw authoritative facts* (timestamps, roster, IPs,
   audio/noise config, seeds — only capturable at runtime) vs *derived labels* (timeline windows,
   flow labels — computed). VM4 writes one `manifest.json` per session of **raw facts only**; a
   separate **versioned** offline script derives labels from `manifest + pcap`. This lets the entire
   dataset be relabeled offline forever without re-running (costly) AWS calls.

7. **Audio realism — seeded turn schedule in the spec; bot gates playback.**
   Continuous dual playback yields two uniform always-on streams — unrealistic and makes
   participant-counting artificially easy. Real conversation is half-duplex (a listening client is
   near-silent; Opus VAD/DTX thins its packets), which is exactly the *hard, realistic* signal
   wanted. VM4 generates a **seeded randomized turn schedule** (speaking windows, gaps) per session
   into the spec; bots play from the shared LibriSpeech source only during their windows, silence/DTX
   otherwise. Reproducible from the seed; dynamics varied without regenerating audio. Whether silent
   windows send digital silence vs stop sending is a second-order knob to tune in testing.

8. **Bot scope — minimal bidirectional VoIP participant; `media_profile` flag.**
   Video/screenshare aren't free no-ops — they inject their own media flows; Deepgram adds outbound
   TLS to a third party. Stripping them is a **correctness requirement** for an audio-VoIP dataset,
   not just cleanup. **Keep:** InitSDK, JWT auth, Join, `JoinVoip()` (bidirectional audio — downlink
   matters and must be preserved), turn-scheduled mic send, S3 heartbeats, clean leave. **Remove:**
   video, virtual camera, screenshare, Deepgram, chat, breakout rooms, PNG/audio disk I/O. Add a
   spec field `media_profile: audio` (default) so a *video-call* class can be a deliberate labeled
   condition later.

9. **Deployment — single container polls S3 and forks a child per session.**
   Smallest delta from the existing image; the bot's SDK is built by the existing
   `Dockerfile`/`CMakeLists.txt`, so running it outside Docker would be *more* work, not less. The
   container's poll loop **forks a child process per session** (the child joins Zoom and dies with
   the session, giving fresh ALSA/Pulse state; the parent keeps polling). `--restart=always`
   self-heals crashes. No systemd / host-side `docker run` orchestration now; can graduate to a
   decoupled systemd agent later with **zero change to the S3 contract**.

10. **Noise — orthogonal to role; VM5-only now, concurrent later; never synthesized.**
    Noise is *not* an exclusive role: a real user is on the call *and* runs background apps. So the
    schema separates two independent axes per VM: `zoom_role` (`host|joiner|none`) and an independent
    `noise` block. **Now:** noise runs on **VM5 only** (`zoom_role: none`); all flows separate
    cleanly by source IP, so there is no same-VM labeling problem. **Later (a config flag-flip):**
    enable `noise` on VoIP VMs and capture the **real** mixed traffic — never fabricate it by running
    iperf elsewhere and rewriting source IPs, which would destroy the real co-host NIC/NAT/timing
    interleaving and risk header artifacts the model could learn. Because downstream features are
    **flow-level (5-tuple)**, the `noise` block records the iperf **target/ports** so mixed-traffic
    VMs remain exactly separable when concurrent noise arrives.

11. **Build scope — n-participants now; phased validation.**
    VM1/VM2/VM3 are all Docker-ready (the handoff was stale: VM3 *is* provisioned like VM1/VM2).
    Build for **n ∈ {2,3}**. Freeze the full S3 contract now (the expensive thing to change later);
    implement/validate the paths incrementally: **2-party no-noise → 3-party no-noise → add VM5
    noise → (future) concurrent noise on VoIP VMs.**

---

## 3. S3 contract (the frozen interface everything keys off)

```
s3://zoom-bot-dataset-s3/
  input_audio/librispeech_audio.pcm              # shared source (do not bake into image)
  sessions/{session_id}/
    spec.json                                     # VM4 -> clients
    heartbeats/{ip}.json                          # each agent/bot -> VM4
    capture.pcap                                  # VM4, post-call
    manifest.json                                 # VM4, post-call (raw facts only)
```

**`spec.json`** (schema frozen now; fields the current phase ignores are still present):
```jsonc
{
  "session_id": "…",
  "meeting": { "id": "…", "pwd": "<redacted>", "zak": "<redacted, host only>" },
  "participant_count": 2,
  "media_profile": "audio",                        // audio | audiovideo (future)
  "roster": [
    { "ip": "10.0.1.119", "zoom_role": "host",
      "noise": { "enabled": false, "profile": null, "target": null, "ports": null,
                 "intensity": null, "source_ips": [] } },
    { "ip": "10.0.2.67",  "zoom_role": "joiner", "noise": { "enabled": false, … } },
    { "ip": "10.0.3.53",  "zoom_role": "joiner", "noise": { "enabled": false, … } },  // 3-party
    { "ip": "10.0.4.16",  "zoom_role": "none",
      "noise": { "enabled": true, "profile": "iperf", "target": "…", "ports": "…",
                 "intensity": "…", "source_ips": [] } }                              // VM5
  ],
  "turns": { "seed": 4711, "windows": [ { "t0": 0.0, "t1": 6.4, "speaker": "10.0.1.119" }, … ] },
  "timing": { "preroll_s": "<rand>", "join_delay_s": "<rand per client>",
              "duration_s": "<rand>", "postroll_s": "<rand>" },
  "seeds": { "turns": 4711, "timing": 9001 }
}
```

**`heartbeats/{ip}.json`** — append-only events with timestamps: `launched` (agent),
`joined` / `left` (bot), `failed` (agent, from non-zero child exit).

**`manifest.json`** — VM4 merges spec + heartbeats + capture metadata into **raw facts**:
`session_id`, `meeting_id`, `roster`, `joins_leaves:[{ip,t_join,t_leave}]`,
`capture:{t_start,t_stop,pcap_key}`, `audio:{seed,…}`, `noise:{…}`, `seeds`. **No derived labels.**

> **Redaction:** meeting `pwd`/`zak` are live credentials — keep them out of any committed manifest
> or doc; store only in the runtime spec object.

---

## 4. End-to-end session sequence

```
VM4 (orchestrator + capture):
  create meeting (REST)  -> id / pwd / zak
  sleep rand(preroll)
  start tshark on ens5 with the client-subnet filter
  write spec.json -> S3                      # clients cannot see session until now

Clients VM1/2/3/5 (single container, polling):
  poll S3; on new spec, match my private IP -> my zoom_role + noise
  fork child:
    if zoom_role != none: auth + Join; play turn-scheduled audio; heartbeat joined/left; leave
    if noise.enabled:     run iperf profile (VM5: this is the only job)

VM4:
  at duration expiry -> REST end meeting      # hard media stop, independent of bot health
  sleep rand(postroll); stop tshark
  merge spec + heartbeats + capture -> manifest.json; upload capture.pcap + manifest.json

Offline (versioned, no AWS):
  labeler(manifest, pcap) -> timeline labels + flow (5-tuple) labels
```

---

## 5. Proposed module structure (refactor of `sample_program/`)

| Location | Module | Responsibility |
|---|---|---|
| `orchestrator/` (VM4) | `session_orchestrator.py` | was `CallSpawner`; schedules, writes spec, drives capture, builds manifest. **Does not spawn bots.** |
| | `meeting_scheduler.py` | keep `MeetingScheduler`; S2S OAuth, create/end meeting, zak. |
| | `capture.py` | tshark start/stop wrapper + the BPF filter. |
| | `turn_schedule.py` | seeded turn-schedule generator. |
| | `manifest.py` | merge spec + heartbeats + capture metadata -> raw-facts manifest. |
| `client/` (VM1/2/3/5) | `agent.py` | poll S3, match IP, fork child / run noise. |
| | `bot.py` | slimmed `meeting_bot.py` (minimal VoIP participant). |
| | `heartbeat.py` | write heartbeat events to S3. |
| | `noise.py` | iperf profile wrapper. |
| `common/` | `schema.py` | spec/manifest dataclasses — **the frozen contract.** |
| | `s3.py` | S3 helpers (boto3 via instance-profile IAM role). |
| `labeler/` (offline) | `derive_labels.py` | timeline + flow labels from manifest + pcap. |

**Delete:** Deepgram path, video/screenshare/camera paths, frame/audio disk I/O, `sample.py`,
the `audio - delete later/` directory.

---

## 6. Verify-in-testing (implementation details, not design forks)

- **chrony / AWS Time Sync (`169.254.169.123`)** enabled on all 5 VMs — timeline labels depend on
  cross-VM clock alignment (sub-ms in-VPC). Confirm when VMs are running.
- **zak only for `zoom_role: host`.** Current code gives the same zak to all bots; joiners should
  join with meeting number + pwd and **no zak** so they register as distinct participants.
- **Downlink audio** actually flows with the stripped bot (bidirectional realism), given the
  documented SDK 6.3.5 `JoinVoip()` workaround.
- **Multiple bots in one meeting** behave as expected (one host via zak, others joiners).
- **Random bounds:** per-client `join_delay ≪ duration` (guarantee N-party overlap); cap
  pre/post-roll so PCAPs don't fill with empty capture.

---

## 7. Phasing / milestones

1. Freeze `common/schema.py` (spec + manifest).
2. Orchestrator: meeting + spec + tshark + manifest (no bots). 2-party.
3. Client agent + slimmed bot; **first end-to-end 2-party no-noise** call (handoff open task #4).
4. 3-party (VM3 joins).
5. Add VM5 iperf noise (separate-VM; flows separate by source IP).
6. Offline labeler.
7. **Future:** concurrent iperf on VoIP VMs (flip `noise.enabled`; capture real mixed traffic);
   multi-ENI source IPs on VM5; `media_profile: audiovideo` class.
8. Infra cleanup carried from handoff: delete `vpc-flow-logs-debug` CloudWatch log group.

---

## Suggested skills for the implementation session

- **`grill-me`** — already used to produce this design; re-invoke only if a new fork appears
  (e.g., the turn-schedule statistical model, or the offline labeler's feature set).
- **`handoff`** — to refresh this doc or `handoff_zoom_aws_setup.md` as decisions evolve.
- **`verify`** / **`run`** — to validate the first end-to-end 2-party call against real behavior
  (downlink audio, distinct-participant joins, capture filter correctness) rather than assuming.
- **`code-review`** — on the refactor diff before generating any dataset, since silent capture/label
  bugs corrupt the dataset invisibly.