from enum import StrEnum


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers


def missing() -> DomainError:
    return DomainError(404, "not_found", "Resource not found.")


class State(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RETRY_WAIT = "RETRY_WAIT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL = {State.SUCCEEDED, State.FAILED, State.CANCELLED, State.TIMED_OUT}
TRANSITIONS = {
    State.CREATED: {State.QUEUED},
    State.QUEUED: {State.RUNNING, State.CANCEL_REQUESTED},
    State.RUNNING: {State.SUCCEEDED, State.FAILED, State.WAITING_FOR_APPROVAL,
                    State.RETRY_WAIT, State.CANCEL_REQUESTED},
    State.WAITING_FOR_APPROVAL: {State.QUEUED, State.FAILED, State.CANCEL_REQUESTED},
    State.RETRY_WAIT: {State.QUEUED, State.CANCEL_REQUESTED},
    State.CANCEL_REQUESTED: {State.CANCELLED},
}


def validate_transition(current: State, target: State) -> None:
    if target == State.TIMED_OUT and current not in TERMINAL:
        return
    if target not in TRANSITIONS.get(current, set()):
        raise DomainError(409, "invalid_transition", f"Cannot move a run from {current} to {target}.")
