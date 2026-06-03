"""Merge a finished session's facts into the raw-facts manifest.

After a call, VM4 has three pieces of truth:

* the ``Spec`` it published (roster, seeds, meeting id),
* the ``HeartbeatEvent`` stream each client reported (joined/left timestamps),
* the ``Capture`` facts (when tshark ran, where the pcap landed).

This module stitches them into one ``Manifest`` of *raw facts only* — no derived
labels (those are computed offline from manifest + pcap) and no credentials (the
``Manifest`` shape has no field for the password or host token). See
REFACTOR_DESIGN.md decision 6.

Pure: give it the three inputs and it returns a ``Manifest``. No AWS, no clock reads.
"""

from __future__ import annotations

from typing import Any

from common.schema import (
    EVENT_JOINED,
    EVENT_LEFT,
    ROLE_NONE,
    Capture,
    HeartbeatEvent,
    JoinLeave,
    Manifest,
    Spec,
)


def build_manifest(
    spec: Spec,
    heartbeats: list[HeartbeatEvent],
    capture: Capture,
    *,
    audio_source: str | None = None,
) -> Manifest:
    """Combine the spec, the reported heartbeats, and the capture facts.

    ``joins_leaves`` is derived from the heartbeats: for each expected joiner (roster
    role other than ``none``) the earliest ``joined`` and latest ``left`` timestamps are
    recorded, or ``None`` if the client never reported that event — a missing join is
    itself a fact worth keeping. ``audio_source`` (the shared source file used) is
    recorded when supplied.
    """
    joins_leaves = _derive_joins_leaves(spec, heartbeats)

    audio: dict[str, Any] = {"seed": spec.seeds.turns}
    if audio_source is not None:
        audio["source"] = audio_source

    return Manifest(
        session_id=spec.session_id,
        meeting_id=spec.meeting.id,  # id only; pwd/zak deliberately not carried
        roster=spec.roster,
        joins_leaves=joins_leaves,
        capture=capture,
        audio=audio,
        noise=_summarize_noise(spec),
        seeds=spec.seeds,
    )


def _derive_joins_leaves(spec: Spec, heartbeats: list[HeartbeatEvent]) -> list[JoinLeave]:
    """One JoinLeave per expected joiner, in roster order, from the heartbeat stream."""
    joins: dict[str, float] = {}
    leaves: dict[str, float] = {}
    for hb in heartbeats:
        if hb.event == EVENT_JOINED:
            # earliest join wins
            if hb.ip not in joins or hb.ts < joins[hb.ip]:
                joins[hb.ip] = hb.ts
        elif hb.event == EVENT_LEFT:
            # latest leave wins
            if hb.ip not in leaves or hb.ts > leaves[hb.ip]:
                leaves[hb.ip] = hb.ts

    result: list[JoinLeave] = []
    for entry in spec.roster:
        if entry.zoom_role == ROLE_NONE:
            continue  # noise/none VMs never join the meeting
        result.append(
            JoinLeave(
                ip=entry.ip,
                t_join=joins.get(entry.ip),
                t_leave=leaves.get(entry.ip),
            )
        )
    return result


def _summarize_noise(spec: Spec) -> dict[str, Any]:
    """Record which VMs ran noise and how, so flows stay separable downstream."""
    sources = [
        {"ip": entry.ip, **entry.noise.to_dict()}
        for entry in spec.roster
        if entry.noise.enabled
    ]
    return {"enabled": bool(sources), "sources": sources}