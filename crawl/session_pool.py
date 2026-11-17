from dataclasses import dataclass
from enum import Enum


class SessionState(str, Enum):
    USABLE = "usable"
    IN_USE = "in_use"
    EXPIRED = "expired"
    DEGRADED = "degraded"
    UNAUTHORIZED_FOR_TARGET = "unauthorized_for_target"
    COOLDOWN = "cooldown"
    NETWORK_FAILED = "network_failed"


class SessionOutcome(str, Enum):
    USABLE = "usable"
    EXPIRED = "expired"
    DEGRADED = "degraded"
    UNAUTHORIZED_FOR_TARGET = "unauthorized_for_target"
    COOLDOWN = "cooldown"
    NETWORK_FAILED = "network_failed"


class SessionPoolExhausted(RuntimeError):
    def __init__(self):
        super().__init__("no usable WeChat sessions remain")


@dataclass(frozen=True)
class SessionLease:
    credential: object
    fakeid: str


class SessionPool:
    def __init__(self, credentials: tuple[object, ...], fakeid: str):
        if not credentials:
            raise ValueError("at least one session credential is required")
        self._credentials = credentials
        self._fakeid = fakeid
        self._states = {credential.session_id: SessionState.USABLE for credential in credentials}

    def lease(self) -> SessionLease:
        for credential in self._credentials:
            if self._states[credential.session_id] == SessionState.USABLE:
                self._states[credential.session_id] = SessionState.IN_USE
                return SessionLease(credential=credential, fakeid=self._fakeid)
        raise SessionPoolExhausted()

    def release(self, lease: SessionLease, outcome: SessionOutcome) -> None:
        session_id = lease.credential.session_id
        if self._states.get(session_id) != SessionState.IN_USE:
            raise RuntimeError("session lease is not active")
        self._states[session_id] = SessionState(outcome.value)

    def mark_usable(self, session_id: str) -> None:
        if session_id not in self._states:
            raise ValueError("unknown session")
        self._states[session_id] = SessionState.USABLE

    def statuses(self) -> dict[str, SessionState]:
        return dict(self._states)
