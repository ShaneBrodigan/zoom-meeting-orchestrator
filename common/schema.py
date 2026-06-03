"""The data shapes VM4 and the subnet clients agree on.

This is the *contract* from REFACTOR_DESIGN.md section 3, written down once so the
orchestrator (which writes ``spec.json`` / ``manifest.json``) and the clients (which
read ``spec.json``) cannot drift apart on the format.

Two documents live here:

* ``Spec``      — VM4 -> clients. Describes one capture session: the meeting to join,
                  who plays which role, the speaking schedule, and the timing. Written
                  to ``sessions/{id}/spec.json``. Carries live credentials, so it lives
                  only in the runtime S3 object and is never committed.
* ``Manifest``  — VM4, post-call. The record of *raw facts* about what actually
                  happened (joins/leaves, capture window, seeds). Written to
                  ``sessions/{id}/manifest.json``. By construction it holds only the
                  meeting *id* — there is no field for the password or host token, so
                  those credentials cannot leak into a saved (and possibly committed)
                  manifest. Derived labels are NOT here; an offline script computes
                  those from manifest + pcap.

Nothing in this module talks to AWS. It is pure shapes plus JSON (de)serialization.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Bump when the shape of Spec or Manifest changes in a way that breaks old readers.
# Stamped into every serialized document so a file always declares which contract
# produced it — important because the dataset is relabeled offline "forever".
SCHEMA_VERSION = 1

# Allowed values for the two independent axes (see REFACTOR_DESIGN.md sections 8, 10).
MEDIA_AUDIO = "audio"
MEDIA_AUDIOVIDEO = "audiovideo"  # future labeled condition
MEDIA_PROFILES = frozenset({MEDIA_AUDIO, MEDIA_AUDIOVIDEO})

ROLE_HOST = "host"
ROLE_JOINER = "joiner"
ROLE_NONE = "none"  # e.g. VM5, which only generates noise
ZOOM_ROLES = frozenset({ROLE_HOST, ROLE_JOINER, ROLE_NONE})


# --------------------------------------------------------------------------- #
# spec.json  (VM4 -> clients)
# --------------------------------------------------------------------------- #

@dataclass
class Meeting:
    """The Zoom meeting to join. ``pwd``/``zak`` are live credentials: they belong
    in the runtime spec only, never in a manifest (the Manifest has no field for them)."""
    id: str
    pwd: str
    zak: str  # host-only capability token; joiners join without it

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "pwd": self.pwd, "zak": self.zak}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Meeting":
        return cls(id=d["id"], pwd=d["pwd"], zak=d["zak"])


@dataclass
class NoiseBlock:
    """Background-traffic config for one VM, independent of its Zoom role.

    Records the iperf target/ports so that, even once noise runs concurrently on a
    VoIP VM, its flows stay separable from the call by 5-tuple."""
    enabled: bool = False
    profile: str | None = None       # e.g. "iperf"
    target: str | None = None        # iperf server address
    ports: str | None = None         # iperf port(s)
    intensity: str | None = None     # profile-specific knob
    source_ips: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "profile": self.profile,
            "target": self.target,
            "ports": self.ports,
            "intensity": self.intensity,
            "source_ips": list(self.source_ips),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NoiseBlock":
        return cls(
            enabled=d.get("enabled", False),
            profile=d.get("profile"),
            target=d.get("target"),
            ports=d.get("ports"),
            intensity=d.get("intensity"),
            source_ips=list(d.get("source_ips", [])),
        )


@dataclass
class RosterEntry:
    """One VM's assignment. The client matches its own private IP against ``ip`` to
    discover its role — network position is the single source of truth."""
    ip: str
    zoom_role: str
    noise: NoiseBlock = field(default_factory=NoiseBlock)

    def __post_init__(self) -> None:
        if self.zoom_role not in ZOOM_ROLES:
            raise ValueError(
                f"zoom_role must be one of {sorted(ZOOM_ROLES)}, got {self.zoom_role!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"ip": self.ip, "zoom_role": self.zoom_role, "noise": self.noise.to_dict()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RosterEntry":
        return cls(
            ip=d["ip"],
            zoom_role=d["zoom_role"],
            noise=NoiseBlock.from_dict(d.get("noise", {})),
        )


@dataclass
class TurnWindow:
    """One speaking window: ``speaker`` (a client IP) plays audio during [t0, t1)."""
    t0: float
    t1: float
    speaker: str

    def to_dict(self) -> dict[str, Any]:
        return {"t0": self.t0, "t1": self.t1, "speaker": self.speaker}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TurnWindow":
        return cls(t0=d["t0"], t1=d["t1"], speaker=d["speaker"])


@dataclass
class Turns:
    """The seeded conversation schedule. Reproducible from ``seed``."""
    seed: int
    windows: list[TurnWindow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"seed": self.seed, "windows": [w.to_dict() for w in self.windows]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Turns":
        return cls(
            seed=d["seed"],
            windows=[TurnWindow.from_dict(w) for w in d.get("windows", [])],
        )


@dataclass
class Timing:
    """Resolved (concrete, not placeholder) randomized timing for the session, in seconds.

    ``join_delay_s`` is per-client (keyed by IP) because each client joins on its own
    offset and the offline labeler needs that per-client truth to build the timeline."""
    preroll_s: float
    duration_s: float
    postroll_s: float
    join_delay_s: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preroll_s": self.preroll_s,
            "duration_s": self.duration_s,
            "postroll_s": self.postroll_s,
            "join_delay_s": dict(self.join_delay_s),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Timing":
        return cls(
            preroll_s=d["preroll_s"],
            duration_s=d["duration_s"],
            postroll_s=d["postroll_s"],
            join_delay_s=dict(d.get("join_delay_s", {})),
        )


@dataclass
class Seeds:
    """The random seeds that make a session reproducible end to end."""
    turns: int
    timing: int

    def to_dict(self) -> dict[str, Any]:
        return {"turns": self.turns, "timing": self.timing}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Seeds":
        return cls(turns=d["turns"], timing=d["timing"])


@dataclass
class Spec:
    """One capture session, VM4 -> clients (``sessions/{id}/spec.json``)."""
    session_id: str
    meeting: Meeting
    participant_count: int
    roster: list[RosterEntry]
    turns: Turns
    timing: Timing
    seeds: Seeds
    media_profile: str = MEDIA_AUDIO
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.media_profile not in MEDIA_PROFILES:
            raise ValueError(
                f"media_profile must be one of {sorted(MEDIA_PROFILES)}, "
                f"got {self.media_profile!r}"
            )

    def entry_for_ip(self, ip: str) -> RosterEntry | None:
        """Return this VM's roster entry, or None if the IP is not in the roster.

        This is how a client self-identifies: read its own private IP, look itself up,
        and obtain its ``zoom_role`` + ``noise`` config."""
        for entry in self.roster:
            if entry.ip == ip:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "meeting": self.meeting.to_dict(),
            "participant_count": self.participant_count,
            "media_profile": self.media_profile,
            "roster": [e.to_dict() for e in self.roster],
            "turns": self.turns.to_dict(),
            "timing": self.timing.to_dict(),
            "seeds": self.seeds.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Spec":
        return cls(
            session_id=d["session_id"],
            meeting=Meeting.from_dict(d["meeting"]),
            participant_count=d["participant_count"],
            roster=[RosterEntry.from_dict(e) for e in d.get("roster", [])],
            turns=Turns.from_dict(d["turns"]),
            timing=Timing.from_dict(d["timing"]),
            seeds=Seeds.from_dict(d["seeds"]),
            media_profile=d.get("media_profile", MEDIA_AUDIO),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "Spec":
        return cls.from_dict(json.loads(text))


# --------------------------------------------------------------------------- #
# heartbeats/{ip}.json  (each agent/bot -> VM4)
# --------------------------------------------------------------------------- #

# Event names a heartbeat may carry (see REFACTOR_DESIGN.md section 3).
EVENT_LAUNCHED = "launched"  # agent forked the child
EVENT_JOINED = "joined"      # bot joined the meeting
EVENT_LEFT = "left"          # bot left the meeting
EVENT_FAILED = "failed"      # agent saw a non-zero child exit
HEARTBEAT_EVENTS = frozenset({EVENT_LAUNCHED, EVENT_JOINED, EVENT_LEFT, EVENT_FAILED})


@dataclass
class HeartbeatEvent:
    """One timestamped event from a client. ``ts`` is epoch seconds (clocks are
    chrony-aligned across VMs, so timestamps are comparable)."""
    event: str
    ip: str
    ts: float

    def __post_init__(self) -> None:
        if self.event not in HEARTBEAT_EVENTS:
            raise ValueError(
                f"event must be one of {sorted(HEARTBEAT_EVENTS)}, got {self.event!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "ip": self.ip, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HeartbeatEvent":
        return cls(event=d["event"], ip=d["ip"], ts=d["ts"])


# --------------------------------------------------------------------------- #
# manifest.json  (VM4, post-call — RAW FACTS ONLY, no derived labels)
# --------------------------------------------------------------------------- #

@dataclass
class JoinLeave:
    """When one client was actually on the call, derived from its heartbeats."""
    ip: str
    t_join: float | None
    t_leave: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"ip": self.ip, "t_join": self.t_join, "t_leave": self.t_leave}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JoinLeave":
        return cls(ip=d["ip"], t_join=d.get("t_join"), t_leave=d.get("t_leave"))


@dataclass
class Capture:
    """The tshark capture window and where the pcap landed in S3."""
    t_start: float
    t_stop: float
    pcap_key: str

    def to_dict(self) -> dict[str, Any]:
        return {"t_start": self.t_start, "t_stop": self.t_stop, "pcap_key": self.pcap_key}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Capture":
        return cls(t_start=d["t_start"], t_stop=d["t_stop"], pcap_key=d["pcap_key"])


@dataclass
class Manifest:
    """The post-call record of raw facts (``sessions/{id}/manifest.json``).

    Holds only ``meeting_id`` — there is deliberately no field for the meeting
    password or host token, so credentials cannot leak into a saved manifest.
    Contains no derived labels; the offline labeler computes those from this file
    plus the pcap.

    ``audio`` and ``noise`` are kept as free-form fact bags for now: their exact
    contents are not yet frozen, and forcing a shape here would be flexibility we
    don't need until those facts settle."""
    session_id: str
    meeting_id: str
    roster: list[RosterEntry]
    joins_leaves: list[JoinLeave]
    capture: Capture
    seeds: Seeds
    audio: dict[str, Any] = field(default_factory=dict)
    noise: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "meeting_id": self.meeting_id,
            "roster": [e.to_dict() for e in self.roster],
            "joins_leaves": [jl.to_dict() for jl in self.joins_leaves],
            "capture": self.capture.to_dict(),
            "audio": dict(self.audio),
            "noise": dict(self.noise),
            "seeds": self.seeds.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        return cls(
            session_id=d["session_id"],
            meeting_id=d["meeting_id"],
            roster=[RosterEntry.from_dict(e) for e in d.get("roster", [])],
            joins_leaves=[JoinLeave.from_dict(jl) for jl in d.get("joins_leaves", [])],
            capture=Capture.from_dict(d["capture"]),
            seeds=Seeds.from_dict(d["seeds"]),
            audio=dict(d.get("audio", {})),
            noise=dict(d.get("noise", {})),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls.from_dict(json.loads(text))