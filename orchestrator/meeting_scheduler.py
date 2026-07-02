"""Create and end the Zoom meeting that anchors one capture session.

VM4 is the only node that talks to Zoom's REST API. This module wraps the
server-to-server (S2S OAuth) dance the old ``sample_program/MeetingScheduler.py``
did, but hands back a schema ``Meeting`` (id / pwd / zak) so the result drops
straight into the frozen contract (REFACTOR_DESIGN.md section 3), and it ends a
meeting on command — the harness's hard media stop for a call (decision 5).

Two deliberate differences from the old demo:

* It returns a ``Meeting`` object, not a loose ``(id, pwd, zak)`` tuple.
* It never logs credentials. The old ``get_zak_token`` printed the live host
  token; the redaction rule (section 3) forbids that, so pwd/zak are never
  printed here, and errors carry only the HTTP status plus Zoom's own (non-secret)
  error message.

The ``zak`` is the *host* capability token. This module simply obtains it; using
it is the host bot's job — joiners join with meeting number + pwd and no zak
(decision, section 6).

The HTTP client is injectable and lazily built, mirroring how ``common/s3.py``
injects boto3: on VM4 the default is a real ``requests`` session; tests pass a
small in-memory fake so the request-building and response-parsing logic runs
without a live Zoom call or real credentials. ``requests`` is imported lazily, so
this module imports fine on machines without it.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Callable

from common.schema import Meeting

# Zoom REST endpoints (account-level server-to-server OAuth).
_OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"
_API_BASE = "https://api.zoom.us/v2"

# S2S access tokens live ~1 hour (Zoom returns the exact lifetime as ``expires_in``).
# We refresh a bit early so a token can never expire mid-request during a long batch —
# the bug that killed the first overnight bulk run at the 60-minute mark.
_TOKEN_REFRESH_MARGIN_S = 60.0
_DEFAULT_TOKEN_LIFETIME_S = 3600.0

# S2S credential environment variables (supplied via .env on VM4; gitignored).
ENV_ACCOUNT_ID = "ZOOM_S2S_ACCOUNT_ID"
ENV_CLIENT_ID = "ZOOM_S2S_CLIENT_ID"
ENV_CLIENT_SECRET = "ZOOM_S2S_CLIENT_SECRET"


class MeetingSchedulerError(RuntimeError):
    """A Zoom REST call failed. Carries the HTTP status, never any credential."""


class MeetingScheduler:
    """VM4's front door to Zoom: create an instant meeting, then end it."""

    def __init__(self, account_id: str, client_id: str, client_secret: str,
                 *, http: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http if http is not None else _make_http_session()
        self._clock = clock
        self._access_token: str | None = None
        self._token_expiry: float = 0.0  # clock() time at which the cached token goes stale

    @classmethod
    def from_env(cls, *, http: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> "MeetingScheduler":
        """Build from the S2S credentials in the environment (the .env on VM4)."""
        try:
            account_id = os.environ[ENV_ACCOUNT_ID]
            client_id = os.environ[ENV_CLIENT_ID]
            client_secret = os.environ[ENV_CLIENT_SECRET]
        except KeyError as missing:
            raise MeetingSchedulerError(
                f"missing Zoom S2S credential in environment: {missing.args[0]}"
            ) from None
        return cls(account_id, client_id, client_secret, http=http, clock=clock)

    # --- front door -------------------------------------------------------- #

    def create_meeting(self, *, topic: str = "Bot Meeting") -> Meeting:
        """Create an instant meeting and return it with the host zak token.

        The meeting has no fixed duration here: the harness ends it via
        :meth:`end_meeting` at the scheduled time (decision 5)."""
        token = self._token()
        body = self._post_json(
            f"{_API_BASE}/users/me/meetings",
            token=token,
            payload={"topic": topic, "type": 1},  # type 1 = instant meeting
            action="create meeting",
        )
        meeting_id = str(body["id"])
        pwd = body.get("password", "")
        zak = self._get_zak(token)
        return Meeting(id=meeting_id, pwd=pwd, zak=zak)

    def end_meeting(self, meeting_id: str) -> None:
        """End the meeting — the call's hard media stop, independent of bot health."""
        token = self._token()
        resp = self._http.put(
            f"{_API_BASE}/meetings/{meeting_id}/status",
            headers=_bearer(token),
            json={"action": "end"},
        )
        _ok(resp, "end meeting")

    # --- internals --------------------------------------------------------- #

    def _token(self) -> str:
        """Return a valid S2S access token, fetching a fresh one when the cached one is stale.

        The token is cached and reused across calls (one round trip covers many meetings),
        but only until it nears expiry: a batch that runs longer than the ~1-hour token
        lifetime would otherwise fail its next REST call with ``HTTP 401: expired``."""
        now = self._clock()
        if self._access_token is not None and now < self._token_expiry:
            return self._access_token
        creds = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        resp = self._http.post(
            f"{_OAUTH_TOKEN_URL}?grant_type=account_credentials"
            f"&account_id={self._account_id}",
            headers={"Authorization": f"Basic {creds}"},
        )
        body = _ok_json(resp, "authenticate")
        token = body.get("access_token")
        if not token:
            raise MeetingSchedulerError("Zoom authenticate returned no access_token")
        lifetime = body.get("expires_in", _DEFAULT_TOKEN_LIFETIME_S)
        self._access_token = token
        self._token_expiry = now + lifetime - _TOKEN_REFRESH_MARGIN_S
        return token

    def _get_zak(self, token: str) -> str:
        resp = self._http.get(
            f"{_API_BASE}/users/me/token?type=zak",
            headers=_bearer(token),
        )
        body = _ok_json(resp, "get zak token")
        zak = body.get("token")
        if not zak:
            raise MeetingSchedulerError("Zoom returned no zak token")
        return zak

    def _post_json(self, url: str, *, token: str, payload: dict[str, Any],
                   action: str) -> dict[str, Any]:
        resp = self._http.post(url, headers=_bearer(token), json=payload)
        return _ok_json(resp, action)


# --- module-level helpers (the only spots that read raw responses) --------- #

def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _ok(resp: Any, action: str) -> None:
    status = getattr(resp, "status_code", None)
    if status is None or status >= 400:
        raise MeetingSchedulerError(_error_message(resp, status, action))


def _ok_json(resp: Any, action: str) -> dict[str, Any]:
    _ok(resp, action)
    return resp.json()


def _error_message(resp: Any, status: Any, action: str) -> str:
    """A clear failure string carrying the status and Zoom's own message — no secrets.

    Zoom's error bodies look like ``{"code": 124, "message": "Invalid access token"}``;
    that message is safe to surface. The request (which carries the credentials) is
    never included."""
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            detail = f": {body['message']}"
    except Exception:
        detail = ""
    shown = status if status is not None else "no response"
    return f"Zoom {action} failed (HTTP {shown}){detail}"


def _make_http_session() -> Any:
    import requests  # imported lazily so the module loads without requests present

    return requests.Session()