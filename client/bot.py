"""A minimal bidirectional VoIP participant — the slimmed ``meeting_bot.py``.

This is the old ~670-line demo bot cut down to exactly what an *audio* VoIP capture
needs (REFACTOR_DESIGN.md decision 8). Stripping the rest is a correctness requirement,
not tidiness: video, a virtual camera, and screenshare each inject their own media
flows, and Deepgram adds outbound TLS to a third party — all of which would pollute a
capture meant to contain only Zoom audio.

**Kept:** InitSDK, JWT auth, Join, the ``JoinVoip()`` work-around (the SDK 6.3.5
regression that otherwise kills raw audio — the downlink realism in decision 8 depends
on it), unmute, a turn-scheduled microphone send from the shared LibriSpeech file, and a
clean leave. **Stripped:** video, virtual camera, screenshare, Deepgram, chat, breakout
rooms, and all PNG / audio disk writes.

Two role rules from REFACTOR_DESIGN.md section 6:

* The **host** joins with the zak (host capability token); **joiners** join with the
  meeting number + password and *no* zak, so Zoom registers them as distinct
  participants (which is what the per-IP capture labels on).
* Audio is **turn-gated**: a bot sends real speech only during its own speaking windows
  and silence otherwise, so the call looks half-duplex (one talker at a time) rather than
  two always-on streams. Whether the silent windows should instead *stop sending* (to let
  Opus DTX thin the stream further) is the second-order knob called out in section 6 — a
  thing to tune against the live SDK, not decide here.

The turn-gating decision — *given the schedule, my IP, and the current session time, am I
the active speaker?* — is a pair of pure module-level functions so it can be unit-tested
with no SDK. Session time is measured from the spec's publish instant (the ``anchor``;
see ``SessionStore.read_spec_with_anchor``), which every client shares.

The Zoom C++ SDK (``zoom_meeting_sdk``) and GLib are imported lazily inside the methods
that touch them — mirroring how ``common/s3.py`` lazy-imports boto3 — so this module
(and its pure-logic tests) import fine on a machine without the SDK installed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from common.schema import ROLE_HOST, Meeting, Turns
from client.heartbeat import HeartbeatRecorder

# The demo's audio constants, kept as-is: 64000-byte chunks sent once a second at a
# 32 kHz mono sampling rate. One second of silence is a same-sized run of zero bytes.
_AUDIO_CHUNK_BYTES = 64000
_AUDIO_SAMPLE_RATE = 32000
_SILENCE_CHUNK = b"\x00" * _AUDIO_CHUNK_BYTES


# --------------------------------------------------------------------------- #
# Pure turn-gating logic (no SDK; unit-tested directly)
# --------------------------------------------------------------------------- #

def speaker_at(turns: Turns, session_time_s: float) -> str | None:
    """The first speaker whose window covers ``session_time_s`` (None in a gap).

    Diagnostic only: windows can overlap (brief double-talk, backchannels), so more
    than one speaker may be active at once — use ``is_my_turn`` to decide whether to
    send audio, not this."""
    for w in turns.windows:
        if w.t0 <= session_time_s < w.t1:
            return w.speaker
    return None


def is_my_turn(turns: Turns, my_ip: str, session_time_s: float) -> bool:
    """True when this client should be sending real audio at ``session_time_s``.

    Checks *any* window covering this moment, not just the first, so an overlapping
    speaker (an overlap handover or a backchannel) is correctly heard as speaking."""
    return any(
        w.t0 <= session_time_s < w.t1 and w.speaker == my_ip for w in turns.windows
    )


def zak_for(zoom_role: str, meeting: Meeting) -> str:
    """The zak to join with: the host token for the host, empty for everyone else.

    Joiners must carry no zak so Zoom treats them as separate participants rather than
    re-admitting the host identity."""
    return meeting.zak if zoom_role == ROLE_HOST else ""


# --------------------------------------------------------------------------- #
# Bot configuration
# --------------------------------------------------------------------------- #

@dataclass
class BotConfig:
    """Everything one bot child needs to join and play. Built by the agent from the
    spec + this client's roster entry, so it is plain data (picklable across the fork)."""
    session_id: str
    meeting: Meeting
    my_ip: str
    zoom_role: str
    turns: Turns
    anchor_epoch: float
    audio_path: str
    join_delay_s: float = 0.0
    display_name: str | None = None

    def name(self) -> str:
        """The in-meeting display name; defaults to ``bot-<ip>`` so participants are
        distinguishable in logs and the Zoom UI."""
        return self.display_name or f"bot-{self.my_ip}"


# --------------------------------------------------------------------------- #
# SDK glue (lazy-imports zoom_meeting_sdk / GLib; not unit-tested)
# --------------------------------------------------------------------------- #

def _generate_jwt(client_id: str, client_secret: str) -> str:
    import jwt

    iat = datetime.utcnow()
    exp = iat + timedelta(hours=24)
    payload = {"iat": iat, "exp": exp, "appKey": client_id,
               "tokenExp": int(exp.timestamp())}
    return jwt.encode(payload, client_secret, algorithm="HS256")


class Bot:
    """The Zoom-facing participant. Drives the SDK; reports join/leave via ``heartbeat``.

    Constructed inside the forked child (see :func:`run_bot`). Everything stateful about
    the call lives here; the gating decision is delegated to the pure functions above so
    the only logic in this class is SDK plumbing.
    """

    def __init__(self, config: BotConfig, heartbeat: HeartbeatRecorder, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._heartbeat = heartbeat
        self._clock = clock

        self.meeting_service = None
        self.setting_service = None
        self.auth_service = None

        self.meeting_service_event = None
        self.auth_event = None

        self.audio_ctrl = None
        self.audio_helper = None
        self.audio_raw_data_sender = None
        self.virtual_audio_mic_event_passthrough = None
        self.audio_source = None
        self.recording_ctrl = None
        self.recording_event = None
        self.participants_ctrl = None
        self.my_participant_id = None

        self._pcm_file = None
        self._joined_reported = False
        self._mic_started = False
        self._priv_requests = 0
        self._last_turn_log_bucket = -1

    # --- lifecycle --------------------------------------------------------- #

    def init(self) -> None:
        """Validate config and bring up the SDK + auth service."""
        import zoom_meeting_sdk as zoom

        if os.environ.get("ZOOM_APP_CLIENT_ID") is None or \
                os.environ.get("ZOOM_APP_CLIENT_SECRET") is None:
            raise RuntimeError(
                "ZOOM_APP_CLIENT_ID / ZOOM_APP_CLIENT_SECRET must be set (SDK JWT auth)"
            )

        init_param = zoom.InitParam()
        init_param.strWebDomain = "https://zoom.us"
        init_param.strSupportUrl = "https://zoom.us"
        init_param.enableGenerateDump = True
        init_param.emLanguageID = zoom.SDK_LANGUAGE_ID.LANGUAGE_English
        init_param.enableLogByDefault = True
        if zoom.InitSDK(init_param) != zoom.SDKERR_SUCCESS:
            raise RuntimeError("InitSDK failed")

        self._create_services()

    def _create_services(self) -> None:
        import zoom_meeting_sdk as zoom

        self.meeting_service = zoom.CreateMeetingService()
        self.setting_service = zoom.CreateSettingService()

        self.meeting_service_event = zoom.MeetingServiceEventCallbacks(
            onMeetingStatusChangedCallback=self._on_meeting_status_changed
        )
        if self.meeting_service.SetEvent(self.meeting_service_event) != zoom.SDKERR_SUCCESS:
            raise RuntimeError("Meeting Service SetEvent failed")

        self.auth_event = zoom.AuthServiceEventCallbacks(
            onAuthenticationReturnCallback=self._on_auth_return
        )
        self.auth_service = zoom.CreateAuthService()
        self.auth_service.SetEvent(self.auth_event)

        auth_context = zoom.AuthContext()
        auth_context.jwt_token = _generate_jwt(
            os.environ["ZOOM_APP_CLIENT_ID"], os.environ["ZOOM_APP_CLIENT_SECRET"]
        )
        if self.auth_service.SDKAuth(auth_context) != zoom.SDKError.SDKERR_SUCCESS:
            raise RuntimeError("SDKAuth call failed")

    def _on_auth_return(self, result):
        import zoom_meeting_sdk as zoom

        if result == zoom.AUTHRET_SUCCESS:
            return self._join_meeting()
        raise RuntimeError(f"Authentication failed: {result}")

    def _join_meeting(self) -> None:
        import zoom_meeting_sdk as zoom

        cfg = self._config
        join_param = zoom.JoinParam()
        join_param.userType = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN
        param = join_param.param
        param.meetingNumber = int(cfg.meeting.id)
        param.userName = cfg.name()
        param.psw = cfg.meeting.pwd
        param.userZAK = zak_for(cfg.zoom_role, cfg.meeting)  # "" for joiners
        param.isVideoOff = True   # audio-only capture: never bring up a camera
        param.isAudioOff = False
        param.isAudioRawDataStereo = False
        param.isMyVoiceInMix = False
        param.eAudioRawdataSamplingRate = zoom.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K

        self.meeting_service.Join(join_param)
        self.setting_service.GetAudioSettings().EnableAutoJoinAudio(True)

    def _on_meeting_status_changed(self, status, result):
        import zoom_meeting_sdk as zoom

        if status == zoom.MEETING_STATUS_INMEETING:
            return self._on_join()
        # The orchestrator ends the meeting as the hard stop; that lands here.
        if status in (zoom.MEETING_STATUS_ENDED, zoom.MEETING_STATUS_FAILED):
            return self.leave()

    def _on_join(self) -> None:
        import zoom_meeting_sdk as zoom

        if self._joined_reported:
            return
        self._joined_reported = True
        self._heartbeat.joined()

        self.participants_ctrl = self.meeting_service.GetMeetingParticipantsController()
        self.my_participant_id = self.participants_ctrl.GetMySelfUser().GetUserID()

        # Host only: auto-grant local-recording privilege to joiners. Every bot needs
        # StartRawRecording() to send mic audio, but a joiner can't self-grant — without
        # this it stays stuck at "requesting" and is silent (host is audible, joiner is
        # not). Turning on auto-allow means a joiner's RequestLocalRecordingPrivilege()
        # is granted immediately, which fires its privilege-changed callback.
        if self._config.zoom_role == ROLE_HOST:
            self._allow_joiner_recording()

        # The SDK-6.3.5 raw-audio work-around: JoinVoip() must be called for audio to
        # flow at all (decision 8). Then unmute so this bot can actually be heard.
        self.audio_ctrl = self.meeting_service.GetMeetingAudioController()
        self.audio_ctrl.JoinVoip()
        self.audio_ctrl.UnMuteAudio(self.my_participant_id)

        # The raw-audio pipeline must be started before the SDK will pull from our
        # external mic source. The working demo calls StartRawRecording() (and
        # subscribe(), see _start_mic) before setExternalAudioSource; dropping them in
        # the refactor is why the bot joined but stayed silent. StartRawRecording needs
        # local-recording privilege — the host has it inherently, a joiner may have to
        # request it from the host — so we attempt it now and retry from the
        # privilege-changed callback rather than assuming it succeeds.
        self.recording_ctrl = self.meeting_service.GetMeetingRecordingController()
        self.recording_event = zoom.MeetingRecordingCtrlEventCallbacks(
            onRecordPrivilegeChangedCallback=self._on_record_privilege_changed,
        )
        self.recording_ctrl.SetEvent(self.recording_event)
        self._schedule(1, self._start_raw_recording)

    def _start_raw_recording(self) -> None:
        """Start the raw-audio pipeline, then bring up the mic. Mirrors the demo:
        if this bot lacks local-recording privilege, request it and bail — the grant
        arrives via :meth:`_on_record_privilege_changed`, which retries."""
        import zoom_meeting_sdk as zoom

        if self._mic_started:
            return
        if self.recording_ctrl.CanStartRawRecording() != zoom.SDKERR_SUCCESS:
            self._priv_requests += 1
            print(f"[bot {self._config.my_ip}] no raw-recording privilege yet; "
                  f"requesting (attempt {self._priv_requests})")
            self.recording_ctrl.RequestLocalRecordingPrivilege()
            # Retry a few times in case this joiner requested before the host had
            # enabled auto-allow; the privilege-changed callback also retries on grant.
            if self._priv_requests < 10:
                self._schedule(3, self._start_raw_recording)
            return
        if self.recording_ctrl.StartRawRecording() != zoom.SDKERR_SUCCESS:
            print(f"[bot {self._config.my_ip}] StartRawRecording failed")
            return
        print(f"[bot {self._config.my_ip}] raw recording started; bringing up mic")
        self._start_mic()

    def _allow_joiner_recording(self) -> None:
        """Host-side: let joiners obtain local-recording privilege automatically, so
        they can StartRawRecording() and send mic audio. Best-effort — a failure here
        must not break the host's own join, so it is logged, not raised."""
        try:
            self.participants_ctrl.AllowParticipantsToRequestLocalRecording(True)
            self.participants_ctrl.AutoAllowLocalRecordingRequest(True)
            print(f"[bot {self._config.my_ip}] host: auto-allow local recording enabled")
        except Exception as err:  # noqa: BLE001 - log, don't crash the join
            print(f"[bot {self._config.my_ip}] host: enabling auto-allow failed: {err}")

    def _on_record_privilege_changed(self, can_record) -> None:
        print(f"[bot {self._config.my_ip}] record privilege changed: can_record={can_record}")
        if can_record:
            self._schedule(1, self._start_raw_recording)

    def _schedule(self, delay_s: int, fn: "Callable[[], None]") -> None:
        """Run ``fn`` once after ``delay_s`` on the GLib loop (the SDK's thread)."""
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        def once():
            fn()
            return False  # one-shot

        GLib.timeout_add_seconds(delay_s, once)

    def _start_mic(self) -> None:
        import zoom_meeting_sdk as zoom

        self._mic_started = True
        self.audio_helper = zoom.GetAudioRawdataHelper()
        if self.audio_helper is None:
            raise RuntimeError("GetAudioRawdataHelper returned None")

        # Subscribe to incoming raw audio with a discard callback. The working demo
        # subscribes before registering the mic, and the mic-start-send callback does
        # not fire reliably without it. The downlink audio already arrives over the
        # JoinVoip media channel, so decoding-and-dropping it here adds no extra network
        # flow and writes nothing to disk — the capture stays audio-only-clean
        # (decision 8).
        self.audio_source = zoom.ZoomSDKAudioRawDataDelegateCallbacks(
            onOneWayAudioRawDataReceivedCallback=self._on_audio_received,
            collectPerformanceData=False,
        )
        self.audio_helper.subscribe(self.audio_source, False)

        self.virtual_audio_mic_event_passthrough = zoom.ZoomSDKVirtualAudioMicEventCallbacks(
            onMicInitializeCallback=self._on_mic_initialize,
            onMicStartSendCallback=self._on_mic_start_send,
        )
        self.audio_helper.setExternalAudioSource(self.virtual_audio_mic_event_passthrough)

    def _on_audio_received(self, data, node_id) -> None:
        """Discard incoming audio. We must subscribe for the send pipeline to start,
        but an audio-only capture keeps no recordings — so this deliberately drops it."""
        return

    def _on_mic_initialize(self, sender) -> None:
        print(f"[bot {self._config.my_ip}] mic initialize")
        self.audio_raw_data_sender = sender

    def _on_mic_start_send(self) -> None:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        import zoom_meeting_sdk as zoom

        print(f"[bot {self._config.my_ip}] mic start send")
        self._open_audio()

        def send_chunk():
            t = self._clock() - self._config.anchor_epoch
            mine = is_my_turn(self._config.turns, self._config.my_ip, t)
            if mine:
                chunk = self._next_audio_chunk()
            else:
                chunk = _SILENCE_CHUNK  # listening: send silence (DTX tuning is a knob)
            self.audio_raw_data_sender.send(chunk, _AUDIO_SAMPLE_RATE,
                                            zoom.ZoomSDKAudioChannel_Mono)
            self._log_turn(t, mine)
            return True

        GLib.timeout_add(1000, send_chunk)

    def _log_turn(self, session_time_s: float, speaking: bool) -> None:
        """Print the speaking/listening state at most once per ~10 s so a run can be
        confirmed (alternating speakers) from the logs, not just by ear."""
        bucket = int(session_time_s // 10)
        if bucket != self._last_turn_log_bucket:
            self._last_turn_log_bucket = bucket
            state = "SPEAKING" if speaking else "silent"
            print(f"[bot {self._config.my_ip}] t={session_time_s:6.1f}s {state}")

    # --- audio source ------------------------------------------------------ #

    def _open_audio(self) -> None:
        import random

        self._pcm_file = open(self._config.audio_path, "rb")
        # Start from a random offset so different bots don't read in lock-step.
        self._pcm_file.seek(0, 2)
        size = self._pcm_file.tell()
        start = max(0, size - _AUDIO_CHUNK_BYTES)
        self._pcm_file.seek((random.randint(0, start) // 2) * 2 if start else 0)

    def _next_audio_chunk(self) -> bytes:
        chunk = self._pcm_file.read(_AUDIO_CHUNK_BYTES)
        if not chunk:
            self._pcm_file.seek(0)
            chunk = self._pcm_file.read(_AUDIO_CHUNK_BYTES)
        return chunk

    # --- teardown ---------------------------------------------------------- #

    def leave(self) -> None:
        import zoom_meeting_sdk as zoom

        if self.meeting_service is None:
            return
        if self.meeting_service.GetMeetingStatus() != zoom.MEETING_STATUS_IDLE:
            self.meeting_service.Leave(zoom.LEAVE_MEETING)
        if self._joined_reported:
            self._heartbeat.left()
            self._joined_reported = False

    def cleanup(self) -> None:
        import zoom_meeting_sdk as zoom

        if self._pcm_file is not None:
            self._pcm_file.close()
            self._pcm_file = None
        if self.audio_helper is not None:
            self.audio_helper.unSubscribe()
        if self.meeting_service is not None:
            zoom.DestroyMeetingService(self.meeting_service)
        if self.setting_service is not None:
            zoom.DestroySettingService(self.setting_service)
        if self.auth_service is not None:
            zoom.DestroyAuthService(self.auth_service)
        zoom.CleanUPSDK()


def run_bot(config: BotConfig, *, store: "SessionStore | None" = None) -> None:  # noqa: F821
    """The forked child's entry point: join the meeting and run until it ends.

    Builds the SDK bot, drives the GLib main loop (the SDK is callback-driven), and
    always leaves + cleans up on the way out so a crash can't strand the bot in the
    meeting. ``store`` is injectable for tests; on a VM it defaults to the IAM-role one.
    """
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from common.s3 import SessionStore

    store = store or SessionStore()
    heartbeat = HeartbeatRecorder(store, config.session_id, config.my_ip)
    bot = Bot(config, heartbeat)

    # The randomized per-client join offset (decision 4): wait, then bring up the SDK so
    # this bot joins partway into the call, staggering the join ramp across subnets.
    if config.join_delay_s > 0:
        time.sleep(config.join_delay_s)

    try:
        bot.init()
        loop = GLib.MainLoop()
        GLib.timeout_add(100, lambda: True)  # keep the loop responsive
        loop.run()
    finally:
        try:
            bot.leave()
            bot.cleanup()
        finally:
            os._exit(0)  # SDK threads don't always unwind cleanly; force the child out