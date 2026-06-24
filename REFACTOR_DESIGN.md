# REFACTOR_DESIGN — Distributed Zoom VoIP Dataset Harness

**Project:** MSc AI Thesis — Classifying Encrypted Zoom VoIP Traffic
**Student:** Shane Brodigan, x24309940, National College of Ireland
**Status:** Design converged (grill-me session, 2026-06-02). Implementation in progress — Phase 1 + Phase 2a–2g done (`common/s3.py` verified against real AWS 2026-06-03; `meeting_scheduler.py` 2e live-verified 2026-06-04; `capture.py` 2f live-verified on VM4 2026-06-04 — real tshark on `ens5` captured only pre-NAT client IPs; note: pcap must be written to `/tmp`, dumpcap drops privileges). `orchestrator/session_orchestrator.py` 2g **built + unit-verified + live-verified on VM4 2026-06-08** (the VM4 conductor; live plumbing run `sess-20260608T150024Z-456d` — 0 captured packets as expected with the client VMs down). A grill-me pass that day refined §4 (recorded pre/post-roll) and decision 10 (noise is an independent record, not a spec trigger). **Phase 3 — the client side (`client/agent.py` + slimmed `client/bot.py` + `client/heartbeat.py`) — built + unit-verified AND the first end-to-end 2-party no-noise call LIVE-VERIFIED on AWS 2026-06-09** (105 tests pass; session `sess-20260609T150600Z-9bda` — VM1 host + VM2 joiner joined as distinct participants [host with zak, joiner without], real `joins_leaves` timestamps in the manifest, 28 MB / 24,371-packet pcap with pre-NAT client IPs + Zoom relay downlink; VM3 idle as a negative control; turn-sync t=0 = spec publish time via `read_spec_with_anchor`). **Step 4 — 3-party — LIVE-VERIFIED on AWS 2026-06-10** (session `sess-20260610T105421Z-0d1f`; VM1 host + VM2 + VM3 joiners, three populated `joins_leaves` with a real ~39 s three-party overlap window, all three left within 0.02 s on the REST hard-stop; pcap 29,485 packets with all three pre-NAT client IPs [`10.0.1.119`/`10.0.2.67`/`10.0.3.53`] + Zoom relay downlink, no `10.0.0.7` twins or SSH; manifest+pcap both clean). **§7 step 5 — VM5 iperf noise — design CONVERGED via grill-me 2026-06-10 (decision 10 + §7 step 5 below; full record in `handovers/handoff_zoom_refactor_phase8.md`), then BUILT + unit-verified + `/code-review`'d 2026-06-10** (`common/noise_config.py` + `common/s3.py` `read_noise_config()` + standalone `client/noise.py` seeded burst/idle iperf loop; both up/download bursts, rate drawn from a range; code-review caught + fixed a `-b 0M`=unlimited rate-floor trap; **121 tests pass**, all local — no AWS yet). Live AWS validation of the noise is deferred to a single combined run *with* the labeler (see phase9). **§7 step 6 — the offline labeler (`labeler/derive_labels.py`) — BUILT + unit-verified + `/code-review`'d 2026-06-11** (timeline from real join/leave facts + flow labels noise/zoom_media/zoom_signaling/other with the firing rule recorded; noise anchored on the typed roster noise blocks incl. `source_ips`; 142 tests, all local). **Labeler LIVE-VALIDATED on a fresh real 3-party capture 2026-06-12** (session `sess-20260612T103058Z-4e80`, deploy/smoke re-run with code unchanged; labeler run on VM4 — timeline ramped 0→1→2→3→2→1→0 with a ~66 s three-party window, no warnings; flows separated into zoom_media/zoom_signaling/other with housekeeping [Ubuntu/Canonical/Cloudflare-NTP] correctly kept OUT of the Zoom labels — the deferred-item-1 pollution check PASSED, so no rule change needed). **§7 step 5 (VM5 noise) LIVE-VALIDATED 2026-06-12 in the combined run — the harness is now feature-complete for the `audio` profile.** Session `sess-20260612T135535Z-23fa`: a 3-party call with VM5 iperf noise running alongside. The noise infra was stood up (dedicated internet `iperf-server` at EIP `108.132.222.246` with 3 listeners; `config/noise.json` in S3; VM5 provisioned natively with iperf3+boto3, chrony confirmed); `client/noise.py` ran live for the first time (seeded TCP/UDP bursts, confirmed flowing pre-NAT on `ens5` as `10.0.4.16 → 108.132.222.246`); the orchestrator recorded the noise block into the manifest (one committed roster edit). pcap clean under heavy noise (no `10.0.0.7` twins/SSH). **The labeler separated noise from Zoom perfectly:** flows `noise:24, zoom_media:39, zoom_signaling:107, other:35`; all 24 noise flows exactly `10.0.4.16 ↔ 108.132.222.246` (rule `noise-vm-to-iperf-server`), zero leakage either way, `warnings:[]`. **All of §7 steps 1–6 are now built AND live-validated.** **Realistic VM5 noise — design converged via grill-me 2026-06-16 + BUILT + unit-verified (Checkpoint 1, 156 tests, all local) 2026-06-16:** VM5 now mixes three weighted traffic profiles (iperf throughput + `curl` web downloads + `ffmpeg` real-time HLS video) instead of iperf alone, so the background is real-app-shaped, not one tool. Because the realistic profiles hit *arbitrary* web hosts, noise labeling moved from a destination anchor to a **source rule** — any flow from a `zoom_role:none` noise VM is noise, any destination — kept alongside the old dst-anchor rule (which is still the *only* way to separate noise on a future concurrent-noise VoIP VM). Reasoning: per-flow classification with **ET-BERT** (consumes raw packet bytes, not loudness/stats), so the win is negative-class byte-pattern *diversity*, not volume; loudness is decoupled from class balance (handled in the modelling repo via train-balanced/eval-at-realistic-prior). **Realistic VM5 noise LIVE-VALIDATED (Checkpoint 2) 2026-06-16:** ffmpeg installed on VM5, the new per-profile `config/noise.json` pushed to S3, and a 3-party call + realistic noise + labeler run clean — session `sess-20260616T151901Z-8249`, `warnings:[]`, 226 flows (`zoom_signaling:107, zoom_media:39, noise:28, other:52`); **both noise rules fired** (`noise-from-noise-vm:26` for curl/video to arbitrary CDNs [Fastly/Cloudflare/Hetzner], `noise-vm-to-iperf-server:2` for iperf), every noise flow sourced only from `10.0.4.16`, **zero leakage both ways**, VM5 NTP correctly kept out as `other`/housekeeping, timeline `0→1→2→3→2→1→0` with a ~69 s three-party window. **The harness is now feature-complete AND live-validated for the realistic-noise `audio` profile; next is bulk dataset generation.** **Phase 16 (2026-06-23):** caught that the bots had been **silent** (the slimmed refactor dropped `StartRawRecording()` + `audio_helper.subscribe`, and joiners lacked recording privilege) — fixed + live-verified by ear; all pre-2026-06-23 captures contain no real speech and are discarded. **Phase 17 (2026-06-24) — built + unit-tested locally (196 tests), live test deferred to next session:** (a) **realistic conversation timing** in `turn_schedule.py` — seeded long pauses (4-6s/10%), brief overlaps (5%), backchannels (10%); windows can now overlap so `bot.is_my_turn` checks *any* covering window (`Turns` shape unchanged, contract untouched); (b) **`labeler/batch_label.py`** — local front door that syncs all sessions, labels each, pushes `labels.json` back to S3, prints OK/FLAG QC; (c) **VM5 noise was silently broken** (a hanging ffmpeg with no timeout wedged the forever-loop; all 4 download URLs dead) — fixed with a per-burst timeout + exception-safe runner + per-burst heartbeat logging + working cloudflare download URLs in `config/noise.json`. **Phase 18 (2026-06-24) — all three Phase-17 changes now LIVE-VALIDATED:** a combined 3-party + VM5-noise session (`sess-20260624T163735Z-c6de`) labeled clean (`warnings:[]`, both noise rules, zero leakage, timeline→3) AND **confirmed by ear** (all three bots audible + alternating, a long pause and a talk-over heard). Also fixed a noise download-throttling problem found en route: cloudflare-only downloads got rate-limited on VM4's shared NAT egress IP (~1 h silent dead window), so `config/noise.json` now spreads downloads across an **11-URL pool over 4 hosts** (tele2/OVH/thinkbroadband/cloudflare) and `client/noise.py` hardened curl (`-f`/`--retry`/`-w`) now **judges a download by bytes moved, not exit code** (a `--max-time` abort pulled megabytes and isn't a failure), plus a `NOISE_SEED_OFFSET` knob for a future 2nd noise generator (202 tests). `launch-guide.md` rewritten concise. **Next: build the noise-mismatch FLAG in `batch_label.py`, then bulk generation.** See the newest file in `handovers/` (`handoff_zoom_refactor_phase18.md`) and `launch-guide.md`.
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
    **Noise is a record, not a trigger** (refined 2026-06-08, grill-me). VM5 runs its iperf load
    **independently** of the per-session spec — on its own schedule — so background traffic is present
    through the pre-roll, post-roll, and gaps. This denies a model the trivial "any traffic = a call"
    shortcut it could otherwise learn from a silent-then-call pcap. The spec's `noise` block therefore
    *records* what VM5 is doing (for 5-tuple separability) but never *starts* it; since the recorded
    pre-roll precedes the spec, a spec-triggered noise could never cover it anyway. The orchestrator
    (VM4) does not launch or stop noise.
    **Converged implementation design (grill-me 2026-06-10; full record in
    `handovers/handoff_zoom_refactor_phase8.md`):**
    - *Generated live on VM5, NOT offline pcap-merged.* An alternative — merge the real Zoom capture with
      an existing real-world background pcap (e.g. `mergecap`) for richer real background — was **rejected**:
      it splices two different capture vantages (different NAT/MTU/TTL/clock → "which camera filmed this"
      artifacts a model cheats on), has **no real co-existence** (independent timelines laid on top of each
      other, none of the real shared-link/NAT interference that is the whole point), and needs IP-rewriting
      (forbidden). The Zoom traffic is already 100% real; noise is only a confounder, so we keep it live
      (real link/NAT/timing, one camera) and accept less behavioural variety rather than buy variety with
      capture artifacts.
    - *iperf now; traffic profile pluggable later.* iperf packets are real; only their *behaviour* is
      synthetic. Adequate for the silence-shortcut job **provided the noise is varied**. Build iperf
      concretely now, keep the schedule loop separate from the iperf call so a real-app profile
      (curl/wget pulls, a video stream) can be added later — **do not pre-build a plugin layer** (no
      swappable layer until a 2nd real profile). Document iperf as a known, defensible scoping limitation.
    - *Dedicated internet iperf server (outside the VPC).* VM5 (iperf client) → VM4 NAT → internet server,
      so noise crosses the edge like real background traffic and appears on `ens5` pre-NAT as
      `10.0.4.16 → <server>`. A stable server IP/ports give the labeler a clean anchor. (Free fallback: an
      iperf server on a private VM — but then the destination never leaves to the internet.)
    - *Standalone program, not the agent.* `noise.py` is its own front door (`python -m client.noise`,
      own container), started once at provisioning and left running. The agent already ignores
      `zoom_role: none`, so it never starts noise — keeping the agent single-purpose.
    - *Seeded random burst/idle loop.* iperf has no scheduler (one transfer then exits), so `noise.py`
      loops: draw random burst length/rate/protocol(TCP|UDP)/port/direction from configured ranges → run
      one iperf → idle a random gap → repeat. Seeded (reproducible), varied (no single learnable
      signature), session-independent (covers pre-roll/gaps/post-roll).
    - *Label noise by source VM (originally destination IP) — keep raw IPs out of features.* The offline
      labeler originally tagged `src=10.0.4.16 & dst=<iperf server>` as noise. **Superseded 2026-06-16 by the
      realistic-noise convergence below:** once VM5 also runs web downloads / video to *arbitrary* hosts there
      is no single destination to anchor on, so the primary rule is now **`noise-from-noise-vm`** — any flow
      whose source is a `zoom_role:none` VM (which by definition never joins Zoom) is noise, regardless of
      destination. The destination-anchor rule (`noise-vm-to-iperf-server`) is **kept** because it is the only
      rule that can separate noise from the call on a *future* concurrent-noise VoIP VM (where the source IP
      also carries Zoom); the source rule is therefore gated on `zoom_role:none` so it can never swallow real
      call traffic. Either way this does **not** teach the model "that IP = noise" because **labeling (the
      researcher's offline answer key, may use oracle knowledge) is separate from features (model inputs)**;
      raw IPs are excluded/anonymized as features — the *same hygiene must apply to the Zoom relay IPs*
      (`170.114.*`/`144.195.*`), which are equally memorizable (and is built into the ET-BERT preprocessing
      pipeline, which masks IP/port). Models must classify by traffic *shape*, not endpoint addresses.
    - *One config in S3 = single source of truth (`config/noise.json`).* Holds the iperf endpoint/ports +
      rate/burst/gap ranges + seed. VM5 reads it to **run**; VM4 reads it to **stamp** the `noise` block
      into each spec/manifest, so the recorded label and the actual traffic cannot drift (a wrong anchor
      would mislabel noise invisibly). Reading a static infra config is not the per-session spec, so
      "never spec-triggered" still holds.
    - *Continuous; manual stop; `--restart=always`; no runtime cap* (a fixed runtime would itself be a
      pattern). *Single source IP now*, structured so extra ENIs are a later flip. *Parameter values are
      tunable in `config/noise.json`*, not hard-coded.
    **Realistic-noise extension (grill-me 2026-06-16; BUILT Checkpoint 1 + LIVE-VALIDATED Checkpoint 2
    2026-06-16, full record in `handovers/handoff_zoom_refactor_phase14.md`):** iperf alone is real packets but synthetic *behaviour*.
    The arrival of two real-app profiles is the trigger decision 10 reserved for splitting "what one burst
    does" out of the burst/idle loop (no plugin layer was built before a 2nd caller existed).
    - *Three weighted profiles, drawn one per burst (sequential, not concurrent).* `iperf` (throughput),
      `download` (`curl` from a curated, pinned set of public big-file URLs), `video` (`ffmpeg -re` pulling a
      public HLS stream at real-time pace, decoded output discarded). Weights + per-profile ranges live in
      `config/noise.json`; the loop, RNG, and seeded reproducibility are unchanged. Concurrent profiles were
      rejected as premature machinery — a *second noise VM* is the cleaner way to add real overlap later.
    - *Tuned for per-flow ET-BERT classification.* ET-BERT tokenizes raw packet *bytes* of a flow's first few
      packets, so (a) each flow ≈ one training example → noise loudness (Mbps) is near-irrelevant to class
      balance, and the real value is **byte-pattern diversity in the negative class** (iperf-UDP's monotonous
      payload vs HTTPS download vs segmented video); (b) bursts must be long enough to yield a real
      handshake + data packets (hence `download.max_time_s` and `video.duration_s` floors), never 1-packet
      blips. The capture already preserves full payloads (no tshark snaplen) — confirmed.
    - *Loudness is not a balance lever.* Train/deploy prior shift (real networks are ~5% Zoom) is handled in
      the modelling repo (train balanced → evaluate at the realistic prior → prior-correct), not by starving
      Zoom in the capture. Loudness is therefore tuned for realistic, varied contention only.
    - *Labeling: see the destination/source bullet above* — primary rule now `noise-from-noise-vm`
      (source = `zoom_role:none` VM), dst-anchor kept for the future concurrent-VoIP flip.

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
  config/noise.json                              # single source of truth for VM5 noise (decision 10):
                                                 #   VM5 reads it to RUN noise; VM4 reads it to STAMP the
                                                 #   spec/manifest noise block. NOT per-session, never a trigger.
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
  start tshark on ens5 with the client-subnet filter   # capture running before anyone can join
  sleep rand(preroll)                        # recorded quiet-with-noise dead-air at the head of the pcap
  write spec.json -> S3                      # clients cannot see session until now

Clients VM1/2/3/5 (single container, polling):
  poll S3; on new spec, match my private IP -> my zoom_role + noise
  fork child:
    if zoom_role != none: auth + Join; play turn-scheduled audio; heartbeat joined/left; leave
    if noise.enabled:     run iperf profile (VM5: this is the only job)

VM4:
  at duration expiry -> REST end meeting      # hard media stop, independent of bot health
  sleep rand(postroll); stop tshark           # recorded tail: call winds down to background-only in the pcap
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
| `client/` (VM1/2/3/5) | `agent.py` | poll S3, match IP, fork **bot** child. Ignores `none`/noise entries — noise is not spec-triggered (decision 10). |
| | `bot.py` | slimmed `meeting_bot.py` (minimal VoIP participant). |
| | `heartbeat.py` | write heartbeat events to S3. |
| | `noise.py` (VM5) | **standalone** seeded burst/idle iperf load generator (decision 10). Own front door (`python -m client.noise`), runs forever independent of any spec; clock + iperf-command edges injected for testing. |
| `common/` | `schema.py` | spec/manifest dataclasses (incl. `NoiseBlock`) — **the frozen contract.** Pure shapes, no AWS. |
| | `noise_config.py` | shared `NoiseConfig`: reads `config/noise.json`; maps into the spec `NoiseBlock`. Read by VM5 (to run) **and** VM4 (to record) — single source of truth (decision 10). |
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
5. Add VM5 iperf noise — **design converged (grill-me 2026-06-10, decision 10); BUILT + unit-verified +
   `/code-review`'d 2026-06-10 (121 tests, all local).** Shipped a **shared `common` `NoiseConfig`**
   (`common/noise_config.py`; reads `config/noise.json` via `SessionStore.read_noise_config()`, maps into
   the spec `NoiseBlock` via `to_noise_block()` — which *is* the VM4-side helper, so no wrapper was added)
   + a **standalone `client/noise.py`** (seeded burst/idle iperf loop, clock + iperf-command edges
   injected; both up/download bursts, rate drawn from a range). `session_orchestrator.py` unchanged.
   Tests cover seed reproducibility, params in range, iperf command line for TCP/UDP/reverse,
   `NoiseConfig`↔`NoiseBlock`, and a rate-floor guard (a sub-0.05 Mbps floor would round to `-b 0M` =
   *unlimited* — rejected at config-load). Noise generated **live on VM5** (not offline-merged), to a
   **dedicated internet iperf server**; labeler separates flows by **destination IP**. **LIVE-VALIDATED
   2026-06-12** in the combined run (`sess-20260612T135535Z-23fa`): infra stood up (`iperf-server` EIP
   `108.132.222.246` + 3 listeners; `config/noise.json` in S3; VM5 native iperf3+boto3, chrony OK);
   `client/noise.py` ran live (pre-NAT `10.0.4.16 → 108.132.222.246`); labeler tagged all 24 noise flows
   `noise`/`noise-vm-to-iperf-server` with zero leakage and no warnings. **Step 5 done — see
   `handovers/handoff_zoom_refactor_phase12.md`.**
   **Realistic-noise extension (2026-06-16, Checkpoint 1 built + Checkpoint 2 LIVE-VALIDATED 2026-06-16):**
   VM5 now mixes `iperf` + `curl` web downloads + `ffmpeg` HLS video (weighted, sequential); labeling gained
   the `noise-from-noise-vm` source rule beside the dst-anchor. Live run `sess-20260616T151901Z-8249`:
   both rules fired (curl/video to arbitrary CDNs caught by the source rule, iperf by the dst-anchor),
   zero leakage, `warnings:[]`. See decision 10 and `handovers/handoff_zoom_refactor_phase14.md`.
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