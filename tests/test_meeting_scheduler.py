"""Checkpoint tests for the meeting scheduler (orchestrator/meeting_scheduler.py).

Local, no live Zoom: a tiny in-memory fake stands in for the ``requests`` session,
so the request-building (URLs, auth headers, instant-meeting body) and the
response-parsing into a schema ``Meeting`` are fully exercised here without a real
OAuth round trip or real credentials. The only thing left to verify live is that
the real Zoom account actually returns a joinable meeting + working zak.

Run with:  pytest tests/test_meeting_scheduler.py
"""

import pytest

from common.schema import Meeting
from orchestrator.meeting_scheduler import (
    ENV_ACCOUNT_ID,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    MeetingScheduler,
    MeetingSchedulerError,
)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeZoom:
    """In-memory stand-in for a requests.Session, routing by URL."""

    def __init__(self, *, token=None, meeting=None, zak=None, end=None):
        self.calls = []  # list of (method, url, headers, json)
        self.token = token or FakeResponse(200, {"access_token": "tok-abc"})
        self.meeting = meeting or FakeResponse(201, {"id": 123456789, "password": "p@ss"})
        self.zak = zak or FakeResponse(200, {"token": "zak-xyz"})
        self.end = end or FakeResponse(204, {})

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers or {}, json))
        if "oauth/token" in url:
            return self.token
        if "/meetings" in url:
            return self.meeting
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers or {}, None))
        if "type=zak" in url:
            return self.zak
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url, headers=None, json=None):
        self.calls.append(("PUT", url, headers or {}, json))
        if "/status" in url:
            return self.end
        raise AssertionError(f"unexpected PUT {url}")


def make_scheduler(fake=None, *, secret="client-secret"):
    fake = fake or FakeZoom()
    sched = MeetingScheduler("acct-1", "client-1", secret, http=fake)
    return sched, fake


def calls_of(fake, method):
    return [c for c in fake.calls if c[0] == method]


# --- create_meeting -------------------------------------------------------- #

def test_create_meeting_returns_meeting_with_id_pwd_zak():
    sched, _ = make_scheduler()
    meeting = sched.create_meeting()
    assert meeting == Meeting(id="123456789", pwd="p@ss", zak="zak-xyz")


def test_meeting_id_is_coerced_to_str():
    # Zoom returns the id as a JSON number; the contract wants a string.
    sched, _ = make_scheduler()
    assert isinstance(sched.create_meeting().id, str)


def test_create_sends_bearer_and_instant_meeting_type():
    sched, fake = make_scheduler()
    sched.create_meeting()
    method, url, headers, body = next(c for c in fake.calls
                                      if c[0] == "POST" and "/meetings" in c[1])
    assert url == "https://api.zoom.us/v2/users/me/meetings"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert body == {"topic": "Bot Meeting", "type": 1}


def test_create_accepts_custom_topic():
    sched, fake = make_scheduler()
    sched.create_meeting(topic="3-party noise run")
    body = next(c[3] for c in fake.calls if c[0] == "POST" and "/meetings" in c[1])
    assert body["topic"] == "3-party noise run"


def test_zak_fetched_from_token_endpoint_with_bearer():
    sched, fake = make_scheduler()
    sched.create_meeting()
    _, url, headers, _ = next(c for c in fake.calls if c[0] == "GET")
    assert url == "https://api.zoom.us/v2/users/me/token?type=zak"
    assert headers["Authorization"] == "Bearer tok-abc"


# --- auth / token caching -------------------------------------------------- #

def test_token_uses_basic_auth_with_account_id():
    sched, fake = make_scheduler()
    sched.create_meeting()
    _, url, headers, _ = next(c for c in fake.calls
                              if c[0] == "POST" and "oauth/token" in c[1])
    assert "account_id=acct-1" in url
    assert "grant_type=account_credentials" in url
    assert headers["Authorization"].startswith("Basic ")


def test_token_fetched_only_once_across_calls():
    sched, fake = make_scheduler()
    sched.create_meeting()
    sched.end_meeting("123456789")
    token_posts = [c for c in fake.calls if c[0] == "POST" and "oauth/token" in c[1]]
    assert len(token_posts) == 1


def test_token_reused_within_its_lifetime():
    # A short hop later, the cached token is still valid -> no second auth round trip.
    now = [1000.0]
    fake = FakeZoom(token=FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600}))
    sched = MeetingScheduler("acct-1", "client-1", "secret", http=fake, clock=lambda: now[0])
    sched.end_meeting("m1")
    now[0] += 60.0  # well inside the ~1h life
    sched.end_meeting("m2")
    token_posts = [c for c in fake.calls if c[0] == "POST" and "oauth/token" in c[1]]
    assert len(token_posts) == 1


def test_token_refreshed_after_it_expires():
    # The overnight-batch bug: past the ~1h token life, the next call must re-auth with a
    # fresh token instead of reusing the stale one (which Zoom rejects with HTTP 401).
    now = [1000.0]
    fake = FakeZoom(token=FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600}))
    sched = MeetingScheduler("acct-1", "client-1", "secret", http=fake, clock=lambda: now[0])

    sched.end_meeting("m1")                       # first auth -> tok-1
    now[0] += 3600.0                              # jump past expiry (1000 + 3600 - 60 margin)
    fake.token = FakeResponse(200, {"access_token": "tok-2", "expires_in": 3600})
    sched.end_meeting("m2")                       # stale -> re-auth -> tok-2

    token_posts = [c for c in fake.calls if c[0] == "POST" and "oauth/token" in c[1]]
    assert len(token_posts) == 2
    puts = calls_of(fake, "PUT")
    assert puts[0][2]["Authorization"] == "Bearer tok-1"
    assert puts[1][2]["Authorization"] == "Bearer tok-2"  # second call used the fresh token


# --- end_meeting ----------------------------------------------------------- #

def test_end_meeting_targets_the_meeting_id():
    sched, fake = make_scheduler()
    sched.end_meeting("987654321")
    _, url, headers, body = next(c for c in fake.calls if c[0] == "PUT")
    assert url == "https://api.zoom.us/v2/meetings/987654321/status"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert body == {"action": "end"}


# --- error handling (clear messages, no credential leaks) ------------------ #

def test_auth_failure_raises_clear_error_without_leaking_secret():
    fake = FakeZoom(token=FakeResponse(401, {"code": 124, "message": "Invalid client"}))
    sched, _ = make_scheduler(fake, secret="super-secret-value")
    with pytest.raises(MeetingSchedulerError) as exc:
        sched.create_meeting()
    msg = str(exc.value)
    assert "401" in msg
    assert "Invalid client" in msg
    assert "super-secret-value" not in msg


def test_create_failure_raises_with_status():
    fake = FakeZoom(meeting=FakeResponse(400, {"message": "Bad Request"}))
    sched, _ = make_scheduler(fake)
    with pytest.raises(MeetingSchedulerError) as exc:
        sched.create_meeting()
    assert "400" in str(exc.value)


def test_end_failure_raises():
    fake = FakeZoom(end=FakeResponse(404, {"message": "Meeting not found"}))
    sched, _ = make_scheduler(fake)
    with pytest.raises(MeetingSchedulerError):
        sched.end_meeting("000")


def test_missing_access_token_raises():
    fake = FakeZoom(token=FakeResponse(200, {}))  # 200 but no token field
    sched, _ = make_scheduler(fake)
    with pytest.raises(MeetingSchedulerError):
        sched.create_meeting()


def test_missing_zak_raises():
    fake = FakeZoom(zak=FakeResponse(200, {}))
    sched, _ = make_scheduler(fake)
    with pytest.raises(MeetingSchedulerError):
        sched.create_meeting()


# --- from_env -------------------------------------------------------------- #

def test_from_env_reads_credentials(monkeypatch):
    monkeypatch.setenv(ENV_ACCOUNT_ID, "env-acct")
    monkeypatch.setenv(ENV_CLIENT_ID, "env-client")
    monkeypatch.setenv(ENV_CLIENT_SECRET, "env-secret")
    fake = FakeZoom()
    sched = MeetingScheduler.from_env(http=fake)
    sched.create_meeting()
    _, url, _, _ = next(c for c in fake.calls
                        if c[0] == "POST" and "oauth/token" in c[1])
    assert "account_id=env-acct" in url


def test_from_env_missing_credential_raises(monkeypatch):
    monkeypatch.delenv(ENV_ACCOUNT_ID, raising=False)
    monkeypatch.delenv(ENV_CLIENT_ID, raising=False)
    monkeypatch.delenv(ENV_CLIENT_SECRET, raising=False)
    with pytest.raises(MeetingSchedulerError):
        MeetingScheduler.from_env(http=FakeZoom())