import json
import logging

from agentos.logging import JsonFormatter
from agentos.scheduler import RetryScheduled
from agentos.telemetry import execution_span, failed, set_outcome


def test_retry_logs_preserve_failure_and_attempt_outcome_without_exporter(caplog):
    with caplog.at_level(logging.INFO, logger="agentos.telemetry"):
        with execution_span("run.attempt", run_id="run-1", attempt=1) as attempt:
            try:
                with execution_span("step.model", run_id="run-1", ordinal=1) as model:
                    failed(model, "provider_rate_limited")
                    raise RetryScheduled()
            except RetryScheduled:
                set_outcome(attempt, "paused")

    step, worker = [json.loads(JsonFormatter().format(record)) for record in caplog.records]
    assert step["error_type"] == "provider_rate_limited"
    assert step["outcome"] == worker["outcome"] == "paused"
    assert worker["run_id"] == step["run_id"] and worker["attempt"] == 1
    assert "error_type" not in worker


def test_handled_failure_is_logged_as_failed(caplog):
    with caplog.at_level(logging.INFO, logger="agentos.telemetry"):
        with execution_span("run.attempt") as attempt:
            failed(attempt, "provider_timeout")

    entry = json.loads(JsonFormatter().format(caplog.records[0]))
    assert entry["outcome"] == "failed" and entry["error_type"] == "provider_timeout"
