from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentos.config import Settings
from agentos.contracts import RunInput, VersionInput
from agentos.domain import DomainError, State, TERMINAL, validate_transition
from agentos.main import create_app
from agentos.runs import fingerprint


def test_replay_fingerprint_uses_validated_request():
    version = uuid4()
    left = RunInput(agent_version_id=version, input={"x": [1, 2], "y": "abc"},
                    deadline_at="2030-01-01T12:00:00Z")
    right = RunInput(agent_version_id=version, input={"y": "abc", "x": [1, 2]}, priority=0,
                     deadline_at="2030-01-01T14:00:00+02:00")
    assert fingerprint(left) == fingerprint(right)
    assert fingerprint(left) != fingerprint(right.model_copy(update={"priority": 1}))


def test_versions_reject_invalid_configurations():
    base = {"instructions": "Respond", "provider": "mock", "model": "deterministic",
            "limits": {"max_steps": 2, "max_tokens": 1000, "timeout_seconds": 15}}
    tool = uuid4()
    for change in [{"instructions": " "}, {"instructions": "a\0b"}, {"model": "missing"},
                   {"provider": "other"}, {"limits": {**base["limits"], "max_steps": 0}},
                   {"tool_version_ids": [tool, tool]}, {"arbitrary": True}]:
        with pytest.raises(ValidationError):
            VersionInput(**{**base, **change})


def test_state_machine():
    for current in TERMINAL:
        for target in State:
            with pytest.raises(DomainError):
                validate_transition(current, target)
    for current in set(State) - TERMINAL:
        validate_transition(current, State.TIMED_OUT)
    for target in (State.RUNNING, State.CANCEL_REQUESTED):
        validate_transition(State.QUEUED, target)
    for target in (State.SUCCEEDED, State.FAILED, State.WAITING_FOR_APPROVAL, State.RETRY_WAIT, State.CANCEL_REQUESTED):
        validate_transition(State.RUNNING, target)
    validate_transition(State.WAITING_FOR_APPROVAL, State.QUEUED)
    validate_transition(State.RETRY_WAIT, State.QUEUED)
    with pytest.raises(DomainError):
        validate_transition(State.QUEUED, State.SUCCEEDED)


def test_invalid_identity_claims_are_rejected_before_database_access(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = tmp_path / "public"
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo))
    password = tmp_path / "db"
    password.write_text("test")
    settings = Settings(db_host="localhost", db_name="test", db_user="test", db_password_file=password,
                        redis_url="redis://localhost", token_public_key_file=public)
    now = datetime.now(UTC)
    claims = {"iss": settings.token_issuer, "aud": settings.token_audience, "sub": "alice",
              "exp": now + timedelta(minutes=10)}
    with TestClient(create_app(settings)) as client:
        for edits in [{"aud": "wrong"}, {"iss": "wrong"}, {"exp": now - timedelta(seconds=1)}, {"sub": None}]:
            signed = jwt.encode({**claims, **edits}, key, algorithm="RS256")
            reply = client.get(f"/v1/tenants/{uuid4()}/agents", headers={"Authorization": f"Bearer {signed}"})
            assert reply.status_code == 401
            assert reply.json()["error"]["request_id"] == reply.headers["X-Request-ID"]
