import { useEffect, useState } from "react";
import { Search } from "lucide-react";
import { useInfiniteQuery } from "@tanstack/react-query";
import {
  canWrite,
  money,
  request,
  short,
  states,
  time,
  usePages,
  useResource,
  type Run,
  type Session,
  type Usage,
} from "./api";
import { Heading, Json, More, Panel, ResultState, RunTable, Status, Table, useAction } from "./ui";

export function Runs({ session: s }: { session: Session }) {
  const [term, setTerm] = useState(""),
    [search, setSearch] = useState(""),
    [status, setStatus] = useState("");
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (status) params.set("status", status);
  const q = usePages<Run>(s, "/runs?" + params);
  return (
    <>
      <Heading title="Runs" />
      <div className="filters">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(term.trim());
          }}
        >
          <label>
            <Search size={16} />
            <input
              aria-label="Run ID or agent name"
              placeholder="Run ID or agent name"
              value={term}
              maxLength={128}
              onChange={(e) => setTerm(e.target.value)}
            />
          </label>
          <button className="refresh">Search</button>
        </form>
        <label className="inline-label">
          State
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All states</option>
            {states.map((st) => (
              <option key={st}>{st}</option>
            ))}
          </select>
        </label>
      </div>
      <Panel title="Execution history">
        <ResultState query={q} />
        {q.data && <RunTable runs={q.data.pages.flatMap((p) => p.items)} />}
        <More query={q} />
      </Panel>
    </>
  );
}

interface EventRow {
  seq: number;
  kind: string;
  created_at: string;
  payload: Record<string, string>;
}
interface Diag {
  duration_seconds: number;
  retry_count: number;
  trace_id: string | null;
  last_attempt: { worker_id: string; number: number; outcome: string | null } | null;
  last_step: { ordinal: number; kind: string; status: string } | null;
}
interface Step {
  id: string;
  tool_name: string | null;
  ordinal: number;
  kind: string;
  status: string;
  result: unknown;
  error_code: string | null;
  dispatch_count: number;
  last_dispatch_error: string | null;
}
interface Checkpoint {
  id: string;
  ordinal: number;
  event_seq: number;
  next_action: string;
  created_at: string;
}
interface Invocation {
  id: string;
  model: string;
  status: string;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: string | null;
  error_code: string | null;
  latency_ms: number | null;
}
interface Attempt {
  number: number;
  worker_id: string;
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
}

function Timeline({ s, path }: { s: Session; path: string }) {
  const [events, setEvents] = useState<EventRow[]>([]),
    [message, setMessage] = useState("Connecting…"),
    [restart, retry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let cursor = 0;
    let timer: ReturnType<typeof setTimeout>;
    setEvents([]);
    async function read() {
      try {
        const r = await fetch("/v1/tenants/" + s.tenant + path + "/events", {
          signal: controller.signal,
          cache: "no-store",
          headers: {
            Authorization: "Bearer " + s.token,
            Accept: "text/event-stream",
            "Last-Event-ID": String(cursor),
          },
        });
        if (!r.ok) {
          setMessage("Stream unavailable (HTTP " + r.status + ").");
          return;
        }
        if (!r.body) throw new Error("No stream");
        setMessage("Live");
        const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "",
          terminal = false;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const line = frame.split("\n").find((l) => l.startsWith("data: "));
            if (!line) continue;
            const row: EventRow = JSON.parse(line.slice(6));
            if (row.seq <= cursor) continue;
            cursor = row.seq;
            terminal = ["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"].includes(row.payload.status);
            setEvents((prev) => [...prev, row]);
          }
        }
        if (terminal) {
          setMessage("Complete");
          return;
        }
      } catch {
        if (controller.signal.aborted) return;
      }
      if (!controller.signal.aborted) {
        setMessage("Reconnecting…");
        timer = setTimeout(() => void read(), 2000);
      }
    }
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [s, path, restart]);
  return (
    <Panel title="Execution timeline" action={<span className="stream-state">{message}</span>}>
      {message.startsWith("Stream unavailable") && (
        <button className="refresh more" onClick={() => retry((n) => n + 1)}>
          Reconnect
        </button>
      )}
      {!events.length ? (
        <p className="empty">Waiting for execution events.</p>
      ) : (
        <ol className="timeline">
          {events.map((e) => (
            <li key={e.seq}>
              <span className="timeline-node" />
              <div>
                <strong>{e.payload.status ? <Status value={e.payload.status} /> : e.kind}</strong>
                <small>
                  {e.kind} · event {e.seq}
                </small>
                {e.kind !== "run.state" && (
                  <code>
                    {Object.entries(e.payload)
                      .map(([k, v]) => k + " = " + v)
                      .join(" · ")}
                  </code>
                )}
              </div>
              <time>{time(e.created_at)}</time>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

function useBoundaries<T>(s: Session, path: string, after: string, seq: (r: T) => number) {
  return useInfiniteQuery({
    queryKey: [s.key, path],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      request<T[]>(s, path + "?limit=50&" + after + "=" + pageParam, { signal }),
    getNextPageParam: (last) => (last.length === 50 ? seq(last[last.length - 1]) : undefined),
    refetchInterval: 5000,
    retry: false,
  });
}
function Steps({ s, path }: { s: Session; path: string }) {
  const steps = useBoundaries<Step>(s, path + "/steps", "after", (r) => r.ordinal),
    cp = useBoundaries<Checkpoint>(s, path + "/checkpoints", "after_event", (r) => r.event_seq);
  return (
    <>
      <Panel title="Model and tool steps">
        <ResultState query={steps} />
        {steps.data && (
          <Table
            headers={["Step", "Kind", "State", "Dispatches", "Result or error"]}
            empty="No steps recorded."
            rows={steps.data.pages.flat().map((r) => ({
              key: r.id,
              cells: [
                r.ordinal,
                r.tool_name ? "Tool · " + r.tool_name : r.kind,
                <Status value={r.status} />,
                r.dispatch_count,
                r.error_code ??
                  r.last_dispatch_error ??
                  (r.result != null ? (
                    <details>
                      <summary>View result</summary>
                      <Json value={r.result} />
                    </details>
                  ) : (
                    "—"
                  )),
              ],
            }))}
          />
        )}
        <More query={steps} />
      </Panel>
      <Panel title="Checkpoints">
        <ResultState query={cp} />
        {cp.data && (
          <Table
            headers={["Event", "Step", "Continuation", "Saved"]}
            empty="No checkpoints recorded."
            rows={cp.data.pages.flat().map((r) => ({
              key: r.id,
              cells: [r.event_seq, r.ordinal, r.next_action, time(r.created_at)],
            }))}
          />
        )}
        <More query={cp} />
      </Panel>
    </>
  );
}
function Calls({ s, path }: { s: Session; path: string }) {
  const q = usePages<Invocation>(s, path + "/usage"),
    a = useResource<Attempt[]>(s, path + "/attempts");
  return (
    <>
      <Panel title="Worker attempts">
        <ResultState query={a} />
        {a.data && (
          <Table
            headers={["Attempt", "Worker", "Started", "Outcome"]}
            empty="No attempts recorded."
            rows={a.data.map((r) => ({
              key: String(r.number),
              cells: [
                r.number,
                <code title={r.worker_id}>{short(r.worker_id)}</code>,
                time(r.started_at),
                r.outcome ?? "In progress",
              ],
            }))}
          />
        )}
      </Panel>
      <Panel title="Model calls">
        <ResultState query={q} />
        {q.data && (
          <Table
            headers={["Model", "State", "Input / output", "USD estimate", "Latency", "Error"]}
            empty="No model invocations recorded."
            rows={q.data.pages
              .flatMap((p) => p.items)
              .map((r) => ({
                key: r.id,
                cells: [
                  r.model,
                  r.status,
                  (r.input_tokens ?? "—") + " / " + (r.output_tokens ?? "—"),
                  r.estimated_cost != null ? money(r.estimated_cost) : "—",
                  r.latency_ms != null ? r.latency_ms + " ms" : "—",
                  r.error_code ?? "—",
                ],
              }))}
          />
        )}
        <More query={q} />
      </Panel>
    </>
  );
}

export function RunDetail({ session: s, id }: { session: Session; id: string }) {
  const path = "/runs/" + id,
    q = useResource<Run>(s, path, 3000),
    diag = useResource<Diag>(s, path + "/diagnostics", 3000),
    u = useResource<Usage>(s, "/usage?run_id=" + id);
  const [tab, setTab] = useState("Timeline"),
    action = useAction(),
    r = q.data,
    d = diag.data;
  return (
    <>
      <a className="back" href="/runs">
        Execution history
      </a>
      <Heading title="Run details">{r && <Status value={r.status} />}</Heading>
      <p className="context-line mono">{id}</p>
      <ResultState query={q} />
      {r && (
        <>
          <Panel title="Execution">
            <dl className="facts">
              <dt>Agent version</dt>
              <dd>{r.agent_version_id}</dd>
              <dt>Started</dt>
              <dd>{time(r.created_at)}</dd>
              {d && (
                <>
                  <dt>Worker / attempt</dt>
                  <dd>
                    {d.last_attempt
                      ? d.last_attempt.worker_id + " / " + d.last_attempt.number
                      : "Awaiting assignment"}
                  </dd>
                  <dt>Latest step</dt>
                  <dd>
                    {d.last_step
                      ? [d.last_step.ordinal, d.last_step.kind, d.last_step.status].join(" · ")
                      : "No steps recorded"}
                  </dd>
                  <dt>Duration / retries</dt>
                  <dd>
                    {Number(d.duration_seconds).toFixed(1)}s · {d.retry_count} retries
                  </dd>
                  <dt>Trace ID</dt>
                  <dd>{d.trace_id ?? "Unavailable"}</dd>
                </>
              )}
              {u.data && (
                <>
                  <dt>Reported usage</dt>
                  <dd>
                    {u.data.input_tokens} input · {u.data.output_tokens} output ·{" "}
                    {money(u.data.estimated_cost_usd)} priced
                  </dd>
                </>
              )}
              {r.retry_at && (
                <>
                  <dt>Retry scheduled</dt>
                  <dd>{time(r.retry_at)}</dd>
                </>
              )}
              {r.failure_reason && (
                <>
                  <dt>Failure reason</dt>
                  <dd>{r.failure_reason}</dd>
                </>
              )}
            </dl>
            <ResultState query={diag} />
            <ResultState query={u} />
            {canWrite(s) &&
              !["CANCELLED", "FAILED", "TIMED_OUT", "SUCCEEDED", "CANCEL_REQUESTED"].includes(r.status) && (
                <button
                  className="refresh more"
                  disabled={action.busy}
                  onClick={() =>
                    void action.perform(async () => {
                      await request(s, path + "/cancel", { method: "POST" });
                      await q.refetch();
                    })
                  }
                >
                  Cancel run
                </button>
              )}
            {action.error}
          </Panel>
          <div className="tabs" aria-label="Run detail views">
            {["Timeline", "Steps & checkpoints", "Calls & attempts", "Input & output"].map((t) => (
              <button key={t} aria-pressed={tab === t} onClick={() => setTab(t)}>
                {t}
              </button>
            ))}
          </div>
          {tab === "Timeline" ? (
            <Timeline s={s} path={path} />
          ) : tab === "Steps & checkpoints" ? (
            <Steps s={s} path={path} />
          ) : tab === "Calls & attempts" ? (
            <Calls s={s} path={path} />
          ) : (
            <div className="overview-pair">
              <Panel title="Submitted input">
                <Json value={r.input} />
              </Panel>
              <Panel title="Final output">
                {r.output == null ? <p className="empty">No output recorded.</p> : <Json value={r.output} />}
              </Panel>
            </div>
          )}
        </>
      )}
    </>
  );
}
