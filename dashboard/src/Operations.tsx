import { useState } from "react";
import { Activity, CheckCheck, CircleAlert, Layers3, ListTree, Timer } from "lucide-react";
import { money, number, short, time, usePages, useResource, type Run, type Session, type Usage } from "./api";
import { Heading, More, Panel, ResultState, RunTable, Status, Table } from "./ui";

interface Counts {
  total: number;
  succeeded: number;
  failed: number;
  running: number;
  queued: number;
  healthy_workers: number;
  pending_approvals: number;
}
interface Worker {
  id: string;
  status: string;
  heartbeat_at: string;
  active_runs: string[];
  open_attempts: number;
  last_error: string | null;
  last_error_at: string | null;
}
interface AnalyticsData {
  since: string;
  days: { day: string; runs: number; failures: number; finished: number; duration: number | null }[];
  models: {
    model: string;
    provider: string;
    calls: number;
    input_tokens: number;
    output_tokens: number;
    unresolved: number;
  }[];
  agents: { id: string; name: string; estimated_cost: number; unpriced: number }[];
  tools: { name: string; steps: number; dispatches: number; failures: number }[];
}

export function Overview({ session: s }: { session: Session }) {
  const counts = useResource<Counts>(s, "/overview"),
    usage = useResource<Usage>(s, "/usage"),
    recent = useResource<{ items: Run[] }>(s, "/runs?limit=6");
  const cards = [
    ["Total runs", "total", ListTree],
    ["Succeeded", "succeeded", CheckCheck],
    ["Failed", "failed", CircleAlert],
    ["Running", "running", Activity],
  ] as const;
  return (
    <>
      <Heading title="Overview">
        <a className="refresh" href="/runs">
          Explore runs
        </a>
      </Heading>
      <ResultState query={counts} />
      {counts.data && (
        <div className="metrics">
          {cards.map(([label, key, Icon]) => (
            <article className={"metric metric-" + key} key={key}>
              <div>
                <span>{label}</span>
                <Icon size={19} />
              </div>
              <strong>{number(counts.data[key])}</strong>
            </article>
          ))}
        </div>
      )}
      <div className="overview-pair">
        <Panel title="Execution queue" action={<Layers3 size={18} />}>
          {counts.data && (
            <dl className="readings">
              <dt>Queued runs</dt>
              <dd>{number(counts.data.queued)}</dd>
              <dt>Healthy tenant workers</dt>
              <dd>{number(counts.data.healthy_workers)}</dd>
              <dt>
                <a href="/approvals">Pending approvals</a>
              </dt>
              <dd>{number(counts.data.pending_approvals)}</dd>
            </dl>
          )}
        </Panel>
        <Panel title="Model usage" action={<a href="/analytics">View analytics</a>}>
          <ResultState query={usage} />
          {usage.data && (
            <>
              <dl className="readings">
                <dt>Reported input tokens</dt>
                <dd>{number(usage.data.input_tokens)}</dd>
                <dt>Reported output tokens</dt>
                <dd>{number(usage.data.output_tokens)}</dd>
                <dt>Priced usage</dt>
                <dd>{money(usage.data.estimated_cost_usd)}</dd>
              </dl>
              {usage.data.unpriced + usage.data.unknown + usage.data.reserved > 0 && (
                <p className="panel-note">
                  {usage.data.unpriced} unpriced · {usage.data.unknown} unknown · {usage.data.reserved}{" "}
                  outstanding
                </p>
              )}
            </>
          )}
        </Panel>
      </div>
      <Panel title="Recent runs" action={<a href="/runs">View all runs</a>}>
        <ResultState query={recent} />
        {recent.data && <RunTable runs={recent.data.items} />}
      </Panel>
    </>
  );
}

export function Workers({ session: s }: { session: Session }) {
  const q = usePages<Worker>(s, "/workers");
  return (
    <>
      <Heading title="Workers" />
      <p className="context-line">
        <Activity size={17} /> Workers with tenant activity <span>·</span> Concurrency limit{" "}
        {s.workspace.concurrency_limit}
      </p>
      <Panel title="Worker sessions">
        <ResultState query={q} />
        {q.data && (
          <Table
            headers={["Worker", "State", "Last heartbeat", "Active executions", "Latest error"]}
            empty="No worker has handled a run for this tenant."
            rows={q.data.pages
              .flatMap((p) => p.items)
              .map((w) => ({
                key: w.id,
                cells: [
                  <code title={w.id}>{short(w.id)}</code>,
                  <Status value={w.status} />,
                  time(w.heartbeat_at),
                  w.active_runs.length
                    ? w.active_runs.map((id) => (
                        <a className="inline-run" key={id} href={"/runs/" + id}>
                          {short(id)}
                        </a>
                      ))
                    : "0",
                  w.last_error ? (
                    <span>
                      {w.last_error}
                      <small>{w.last_error_at && time(w.last_error_at)}</small>
                    </span>
                  ) : (
                    "—"
                  ),
                ],
              }))}
          />
        )}
        <More query={q} />
      </Panel>
    </>
  );
}

export function Analytics({ session: s }: { session: Session }) {
  const [period, setPeriod] = useState(7);
  const q = useResource<AnalyticsData>(s, "/analytics?days=" + period),
    d = q.data;
  const finished = d?.days.reduce((n, r) => n + r.finished, 0) ?? 0,
    failed = d?.days.reduce((n, r) => n + r.failures, 0) ?? 0;
  const duration = d?.days.reduce((n, r) => n + (r.duration ?? 0) * r.finished, 0) ?? 0;
  const max = Math.max(1, ...(d?.days.map((r) => r.runs) ?? []));
  return (
    <>
      <Heading title="Usage & analytics">
        <label className="inline-label">
          Period
          <select value={period} onChange={(e) => setPeriod(Number(e.target.value))}>
            {[7, 30, 90].map((n) => (
              <option key={n} value={n}>
                Last {n} UTC days
              </option>
            ))}
          </select>
        </label>
      </Heading>
      <ResultState query={q} />
      {d && (
        <>
          <div className="metrics analytics-metrics">
            <article className="metric">
              <div>
                Finished runs
                <CheckCheck size={18} />
              </div>
              <strong>{number(finished)}</strong>
            </article>
            <article className="metric">
              <div>
                Failure rate
                <CircleAlert size={18} />
              </div>
              <strong>{finished ? ((100 * failed) / finished).toFixed(1) + "%" : "—"}</strong>
            </article>
            <article className="metric">
              <div>
                Mean duration
                <Timer size={18} />
              </div>
              <strong>{finished ? (duration / finished).toFixed(1) + " s" : "—"}</strong>
            </article>
          </div>
          <Panel title="Daily execution volume" action={<span className="muted">UTC</span>}>
            {d.days.length ? (
              <div className="trend">
                {d.days.map((r) => (
                  <div className="trend-row" key={r.day}>
                    <time>{r.day}</time>
                    <div className="track">
                      <i style={{ width: (100 * r.runs) / max + "%" }} />
                    </div>
                    <span>
                      {r.runs} runs · {r.failures} failed
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="empty">No executions in this period.</p>
            )}
          </Panel>
          <Panel title="Tokens by model">
            <Table
              headers={["Model", "Provider", "Calls", "Input", "Output", "Unresolved"]}
              empty="No model calls in this period."
              rows={d.models.map((r) => ({
                key: r.provider + "/" + r.model,
                cells: [
                  r.model,
                  r.provider,
                  r.calls,
                  number(r.input_tokens),
                  number(r.output_tokens),
                  r.unresolved,
                ],
              }))}
            />
          </Panel>
          <div className="overview-pair">
            <Panel title="Cost by agent">
              <Table
                headers={["Agent", "Priced usage", "Unpriced calls"]}
                empty="No agent usage in this period."
                rows={d.agents.map((r) => ({
                  key: r.id,
                  cells: [<a href={"/agents/" + r.id}>{r.name}</a>, money(r.estimated_cost), r.unpriced],
                }))}
              />
            </Panel>
            <Panel title="Tool usage">
              <Table
                headers={["Tool", "Steps", "Dispatches", "Failed"]}
                empty="No tool activity in this period."
                rows={d.tools.map((r) => ({
                  key: r.name,
                  cells: [r.name, r.steps, r.dispatches, r.failures],
                }))}
              />
            </Panel>
          </div>
          <p className="panel-note">
            Failure rate includes failed and timed-out runs among finished executions submitted in the
            selected period.
          </p>
        </>
      )}
    </>
  );
}
