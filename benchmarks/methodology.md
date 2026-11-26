# Reproduce the Phase 9 benchmark

From the repository root, run the commands below with Docker Compose, Python 3.12+, the locked backend development dependencies and Playwright Chromium installed. The integration test creates a PostgreSQL container. The acceptance script creates a randomly named Compose project, allocates loopback ports and removes that project's containers and volumes on exit. The target gateway and model fixture are fixed services inside its network.

```sh
cd backend
uv run --locked pytest -q tests integration ../scripts/test_benchmark.py
cd ..
python scripts/check_foundation.py --browser --observability --benchmark-report test-results/phase9.json
```

To measure a fresh stack without first warming it with the regression suite, run the following command. This mode includes the real worker-loss and redelivery checks.

```sh
python scripts/check_foundation.py --observability --benchmark-only --benchmark-report test-results/fresh.json
```

The JSON includes individual k6 timings and status codes, database-derived run timings, first-claim worker IDs, sampled resources, aggregate usage, SQL profiles, broker redelivery and recovery observations. CI saves it as the `phase9-measurements` artifact. An installed Chrome can be selected with `AGENTOS_BROWSER_CHANNEL=chrome`. [Local test setup](../docs/operations/local-development.md)

## Workloads

| Workload | Offered traffic | Verified outcome |
| --- | --- | --- |
| Sequential submissions | One k6 virtual user submits 100 independent receipt runs without waiting for each execution to finish | 100 successes with one attempt each |
| Submission burst | 100 virtual users each submit one receipt | 100 successes, two competing worker processes, one attempt each |
| Multi-step | 10 virtual users submit one model-backed run each. The fixture delays every response by 0.5 s | Each run completes 25 model calls and 24 text-length steps |
| Provider timeout | 10 virtual users submit one run each. The fixture responds after 2 s against a 1 s worker timeout | Each run fails with the provider timeout code after three attempts. Usage remains unknown |
| Worker loss | Kill a worker or interrupt its database connection at five execution boundaries in the recovery suite | Record elapsed time from the expired lease to the next claim and check checkpoint, attempt and usage history |
| Redelivery | Commit a receipt, negatively acknowledge the wakeup, and fetch the same JetStream sequence again | Its delivery count increases and the replacement claim finds no work. PostgreSQL retains one completed attempt |

The load tenant has four execution slots and two workers. Its admission limits permit the full burst. Inference uses a local deterministic HTTP fixture with configured rates of $1 per million input tokens and $2 per million output tokens. The suite also exercises role enforcement, cross-tenant isolation, approval replay, tool revocation and recovery after a protected effect. It checks correctness independently of machine speed.

## Metric definitions

- **Submission latency** is k6 HTTP request duration through the dashboard gateway until the API responds with the committed run. Database dispatch measures time from the first committed queue event's timestamp to the first attempt's start. Completion measures run creation to terminal update and includes queueing and retries.
- **Throughput** is terminal runs divided by the database interval from the earliest creation to the latest terminal update in the cohort. **Slot occupancy** divides summed attempt durations, including waiting on provider I/O, by that interval times the available slots. Docker CPU is a separate measurement.
- **Percentiles** use nearest rank. p99 is included only for cohorts with at least 100 observations. Recovery is reported per injected fault because the faults have different causes. The recovery interval includes any time spent starting replacement workers.
- **Resource samples** cover the load generator's startup, execution and final status check. Each sample retains Docker's CPU percentage and memory working set for the API, two workers, publisher, gateway, model peer, database, Redis and NATS. Short spikes can fall between samples. Values above 100% CPU represent use of multiple cores.
- **SQL profile** uses PostgreSQL's `pg_stat_statements`, reset before each workload. It records the five highest cumulative execution times for the API, worker and publisher database roles. Concurrent statement times can overlap. The capture includes ordinary background activity on the isolated stack.
- **Costs** use reported tokens and the fixture price snapshot. Outstanding or unknown usage does not enter the priced total. The timeout cohort's run failures are intentional and are reported separately from request errors.
- **Investigation targets** are p95 submission below 300 ms, normal dispatch below 2 s and recovery within 30 s of lease expiry. Results are specific to workload, machine and instrumentation. [Architecture targets](../docs/architecture/overview.md)
