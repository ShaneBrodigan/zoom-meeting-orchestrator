# What a Batch Captures — in plain terms

This explains, without jargon, what the harness actually records every time we run a
batch of Zoom calls to build the dataset. For *how* to run a batch see `launch-guide.md`;
for the reasoning behind the design see `REFACTOR_DESIGN.md`. **Keep this file current** —
if the capture behaviour changes (call length, party sizes, noise, what we keep per
packet), update it here.

## The short version

We run real Zoom voice calls between our own bots on separate machines, and we record the
network traffic those calls produce. Each call becomes one saved bundle in cloud storage.
The goal is a labelled dataset: encrypted Zoom voice traffic, mixed with realistic
background internet traffic, with an honest answer key saying which is which.

## What one call looks like

- **Real bots on separate machines.** One bot hosts the meeting; one or two others join.
  They're on different networks, so the traffic looks like real people on a real call — not
  one computer talking to itself.
- **A real conversation.** The bots take turns "speaking" from a shared speech recording,
  with natural touches: pauses, the occasional talk-over, short interjections. So it sounds
  and behaves like a genuine half-duplex conversation, not two constant streams.
- **Background noise, always on.** A separate machine (never on the call) continuously
  generates ordinary internet activity — file downloads, video streaming, and raw
  throughput tests — to arbitrary public sites. This stops the models learning a lazy
  shortcut like "any traffic at all = a call."
- **A quiet lead-in and tail.** Recording starts before anyone joins and stops a little
  after everyone leaves, so each recording begins and ends with background-only traffic.

## What gets saved per call

Each call is saved as one bundle in cloud storage (`s3://…/sessions/<id>/`):

- **The traffic recording (`capture.pcap`)** — every packet seen on the wire during the
  call. Crucially, we only keep the **first 256 bytes of each packet**, not the whole
  thing. That's enough to see who's talking to whom and the shape of the traffic (which is
  all the models use), while keeping the files ~5× smaller. The content of the calls is
  encrypted by Zoom regardless, so we never see or store anyone's actual speech.
- **The facts sheet (`manifest.json`)** — a plain record of what happened: who joined, the
  exact join/leave times, the call length, and the noise settings. Facts only — no guesses,
  no labels, and no passwords.

## What the batch varies (so the dataset is balanced)

Set in `config/generation_plan.json`:

- **Call length** — mix of 5, 10, 15, 20, and 30-minute calls (equal share), with a little
  random jitter.
- **How many on the call** — a 50/50 mix of 2-person and 3-person calls.
- **Who hosts** — rotated across the machines.

Everything is driven by a random seed that's written into each bundle, so any batch can be
reproduced exactly.

## The answer key (made later, offline)

The recordings on their own are just packets. Afterwards, on the laptop (no cloud, no cost),
a separate step reads each call's facts sheet + recording and writes the **answer key**:

- **A timeline** — how many people were on the call at each moment (0 before anyone joins,
  ramping up as they join, back to 0 at the end).
- **A traffic list** — every network conversation sorted into **Zoom** (the real call),
  **noise** (the background activity), or **other** (harmless housekeeping like clock
  syncing). This uses inside knowledge (e.g. which machine makes the noise) — which is fine,
  because it's only the marking scheme; the models never see it.

## What we deliberately do *not* capture

- No video, screen sharing, or camera — this is a voice-only dataset by design.
- No actual audio content — Zoom encrypts it, and we only keep packet headers anyway.
- No passwords or host tokens in any saved file.
- No control chatter between our own machines (that rides separate cloud storage, invisible
  to the capture) and no remote-login traffic.