# Phase 9 reliability and performance results

All 220 submissions returned HTTP 201. All 210 receipt and multi-step runs succeeded. Each of the ten runs with an injected provider timeout failed with the expected error after three attempts. Recovery stayed within the 30-second investigation target in all five crash cases. Queue dispatch exceeded the 2-second target in every workload.

The fresh-stack baseline started on 26 November 2026 at 17:08 UTC. Docker Desktop 29.9.3 had 12 logical CPUs and 3.53 GiB of memory on a Windows 11 host with an AMD Ryzen 5 5600H. The stack used PostgreSQL 17.12, k6 1.3.0, two workers, four execution slots and the observability overlay. Each workload ran once against a single tenant. [Raw observations](baseline.json) · [Reproduction and metric definitions](methodology.md)

## Latency and throughput

Values in each latency column are **p50 / p95 / p99**. The ten-run cohorts show p50 / p95 only. Submission measures the HTTP request through the gateway. Dispatch ends at the first worker claim. Completion includes queueing, execution and retries.

| Workload | Runs | Submission (ms) | First dispatch (s) | Completion (s) | Terminal runs/s | Failed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Sequential submissions | 100 | 20 / 44 / 72 | 34.15 / 125.14 / 133.26 | 34.16 / 125.15 / 133.27 | 0.726 | 0% |
| 100-client burst | 100 | 6,381 / 7,551 / 7,602 | 35.45 / 130.54 / 138.64 | 35.47 / 130.56 / 138.65 | 0.677 | 0% |
| Multi-step | 10 | 252 / 430 | 16.01 / 32.16 | 29.67 / 45.78 | 0.217 | 0% |
| Provider timeout | 10 | 181 / 334 | 2.57 / 4.94 | 18.04 / 28.11 | 0.353 | 100% (injected) |

Only sequential submissions met the 300 ms p95 submission target. The tenant admission lock accounted for the most database execution time during the burst. Its 100 acquisitions accumulated 93.01 seconds across concurrent requests. This identifies contention in the transaction that protects quota enforcement and idempotency. [Admission implementation](../backend/agentos/runs.py)

The two workers split first claims 55/45 for sequential receipts and 54/46 for the burst. Both workloads occupied less than 1.5% of available slot time. Over the final 25 intervals between consecutive completions, the median gap was 2.05 seconds for sequential receipts and 2.04 seconds for the burst.

Those gaps closely match the publisher’s two-second repair interval. During the sequential drain, a database spot check found 15 queued runs, zero active attempts, zero unpublished outbox events and two healthy workers. One explanation to test is that slots consume wakeups while a claim is temporarily unavailable and then wait for the repair scan. Confirming that explanation requires counting skipped claims by reason. [Worker claim loop](../backend/agentos/worker.py) · [Scheduler](../backend/agentos/scheduler.py) · [Publisher repair scan](../backend/agentos/publisher.py)

## Resources and model usage

The peaks below are the highest sums across monitored containers within a single Docker sample. The collection includes the model fixture and gateway. k6 and the monitoring services are outside that sum. CPU percentages can exceed 100% when several cores are active.

| Workload | Samples | Slot occupancy | Peak CPU | Peak memory (MiB) |
| --- | ---: | ---: | ---: | ---: |
| Sequential submissions | 68 | 0.32% | 296.86% | 512.85 |
| 100-client burst | 69 | 1.47% | 347.03% | 517.86 |
| Multi-step | 23 | 75.42% | 281.80% | 538.57 |
| Provider timeout | 15 | 27.49% | 217.29% | 541.03 |

The multi-step cohort completed 250 model calls and 240 tool steps. The fixture reported 3,000 input tokens and 1,000 output tokens, yielding **$0.005** at the configured fixture rates. Receipt runs made no model calls. All 30 timed-out invocations had unknown usage and cost.

## Failure and redelivery checks

| Injected fault | Lease expiry to replacement claim | Final state |
| --- | ---: | --- |
| Before model dispatch | 8.14 s | Succeeded |
| After a keyed tool effect | 4.59 s | Succeeded |
| Database interruption during lease renewal | 21.52 s | Succeeded |
| After the final checkpoint | 8.74 s | Succeeded |
| After the provider replied, before settlement | 8.75 s | Failed with `model_outcome_unknown` |

The last case preserved a single unknown invocation. In the broker test, JetStream delivered the same stored message twice and PostgreSQL recorded one successful attempt. The approval suite also verified that refused, expired, cancelled, revoked and changed actions produced zero protected dispatches. [Recovery assertions](../scripts/check_recovery.py) · [Redelivery fixture](../scripts/fixtures/redelivery.py) · [Approval checks](../scripts/check_approvals.py)
