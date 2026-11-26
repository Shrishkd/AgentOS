import { useRef, useState, type FormEvent } from "react";
import { Plus, Play } from "lucide-react";
import {
  canWrite,
  navigate,
  request,
  short,
  time,
  usePages,
  useResource,
  type Agent,
  type Run,
  type Session,
  type Tool,
  type Version,
} from "./api";
import { Heading, More, Panel, ResultState, Status, Table, useAction } from "./ui";

export function Agents({ session: s }: { session: Session }) {
  const q = usePages<Agent>(s, "/agents"),
    [creating, setCreating] = useState(false),
    action = useAction();
  function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    void action.perform(async () => {
      const agent = await request<Agent>(s, "/agents", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(data)),
      });
      navigate("/agents/" + agent.id);
    });
  }
  return (
    <>
      <Heading title="Agents">
        {canWrite(s) && (
          <button className="primary" onClick={() => setCreating(!creating)}>
            <Plus size={16} />
            Create agent
          </button>
        )}
      </Heading>
      {creating && (
        <Panel title="New agent">
          <form className="editor" onSubmit={create}>
            <label>
              Name
              <input name="name" maxLength={128} required autoFocus />
            </label>
            <label>
              Description
              <textarea name="description" maxLength={4096} />
            </label>
            {action.error}
            <button className="primary" disabled={action.busy}>
              Save agent
            </button>
          </form>
        </Panel>
      )}
      <Panel title="Agent registry">
        <ResultState query={q} />
        {q.data && (
          <Table
            headers={["Agent", "Description", "Registered"]}
            empty="No agents are registered in this workspace."
            rows={q.data.pages
              .flatMap((p) => p.items)
              .map((a) => ({
                key: a.id,
                cells: [<a href={"/agents/" + a.id}>{a.name}</a>, a.description || "—", time(a.created_at)],
              }))}
          />
        )}
        <More query={q} />
      </Panel>
    </>
  );
}

function VersionForm({ session: s, path, done }: { session: Session; path: string; done: () => void }) {
  const tools = usePages<Tool>(s, "/tools"),
    [provider, setProvider] = useState("mock"),
    action = useAction();
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const body = {
      instructions: f.get("instructions"),
      provider,
      model: provider === "mock" ? "deterministic" : provider === "builtin" ? "receipt" : f.get("model"),
      limits: {
        max_steps: Number(f.get("steps")),
        max_tokens: Number(f.get("tokens")),
        timeout_seconds: Number(f.get("timeout")),
        max_cost_usd: f.get("cost") ? Number(f.get("cost")) : null,
      },
      tool_version_ids: provider === "builtin" ? [] : f.getAll("tools"),
    };
    void action.perform(async () => {
      await request(s, path, { method: "POST", body: JSON.stringify(body) });
      done();
    });
  }
  return (
    <Panel title="New version">
      <form className="editor" onSubmit={submit}>
        <label>
          Instructions
          <textarea name="instructions" required maxLength={32000} />
        </label>
        <div className="form-grid">
          <label>
            Provider
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="mock">Mock (deterministic)</option>
              <option value="builtin">Built-in receipt</option>
              <option value="openai-compatible">OpenAI-compatible</option>
            </select>
          </label>
          {provider === "openai-compatible" && (
            <label>
              Model name
              <input name="model" required maxLength={128} />
            </label>
          )}
        </div>
        <div className="form-grid">
          {[
            ["Step limit", "steps", 1, 1000, 8],
            ["Token limit", "tokens", 1, 10000000, 20000],
            ["Timeout (seconds)", "timeout", 1, 604800, 300],
          ].map(([label, name, min, max, value]) => (
            <label key={name}>
              {label}
              <input type="number" name={String(name)} min={min} max={max} defaultValue={value} required />
            </label>
          ))}
          <label>
            Cost limit (USD, optional)
            <input type="number" name="cost" min="0.00000001" max="100000" step="any" />
          </label>
        </div>
        {provider !== "builtin" && (
          <fieldset>
            <legend>Tool permissions</legend>
            <ResultState query={tools} />
            {tools.data &&
              (!tools.data.pages.some((p) => p.items.some((t) => t.enabled)) ? (
                <p>No enabled tools on this page.</p>
              ) : (
                tools.data.pages
                  .flatMap((p) => p.items)
                  .filter((t) => t.enabled)
                  .map((t) => (
                    <label className="check-label" key={t.id}>
                      <input type="checkbox" name="tools" value={t.id} />
                      {t.name} · v{t.version}
                    </label>
                  ))
              ))}
            <More query={tools} />
          </fieldset>
        )}
        {action.error}
        <button className="primary" disabled={action.busy}>
          Save draft version
        </button>
      </form>
    </Panel>
  );
}

function VersionCard({ v, s, path, refresh }: { v: Version; s: Session; path: string; refresh: () => void }) {
  const [runForm, showRun] = useState(false),
    action = useAction(),
    submission = useRef({ body: "", key: "" });
  function launch(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const raw = String(new FormData(e.currentTarget).get("input"));
    void action.perform(async () => {
      let input;
      try {
        input = JSON.parse(raw);
      } catch {
        throw new Error("Enter valid JSON for the run input.");
      }
      if (input === null || Array.isArray(input) || typeof input !== "object")
        throw new Error("Run input must be a JSON object.");
      const body = JSON.stringify({ agent_version_id: v.id, input });
      if (body !== submission.current.body) submission.current = { body, key: crypto.randomUUID() };
      const run = await request<Run>(s, "/runs", {
        method: "POST",
        headers: { "Idempotency-Key": submission.current.key },
        body,
      });
      navigate("/runs/" + run.id);
    });
  }
  function change(op: string) {
    void action.perform(async () => {
      await request(s, path + "/" + v.id + "/" + op, { method: "POST" });
      refresh();
    });
  }
  return (
    <article className="version">
      <div className="version-head">
        <strong>Version {v.number}</strong>
        <Status value={v.state} />
        <code title={v.id}>{short(v.id)}</code>
      </div>
      <p>
        {v.provider} · {v.model}
      </p>
      <details>
        <summary>Configuration and grants</summary>
        <dl className="facts">
          <dt>Instructions</dt>
          <dd className="preserve">{v.instructions}</dd>
          <dt>Execution limits</dt>
          <dd>
            {v.limits.max_steps} steps · {v.limits.max_tokens.toLocaleString()} tokens ·{" "}
            {v.limits.timeout_seconds}s{v.limits.max_cost_usd != null ? " · $" + v.limits.max_cost_usd : ""}
          </dd>
          <dt>Tool version IDs</dt>
          <dd>{v.tool_version_ids.join(", ") || "No grants"}</dd>
        </dl>
      </details>
      {canWrite(s) && (
        <div className="actions">
          {v.state === "DRAFT" && (
            <button className="primary" disabled={action.busy} onClick={() => change("publish")}>
              Publish version
            </button>
          )}
          {v.state === "PUBLISHED" && (
            <>
              <button className="primary" onClick={() => showRun(!runForm)}>
                <Play size={14} />
                Start run
              </button>
              <button className="refresh" disabled={action.busy} onClick={() => change("deactivate")}>
                Deactivate version
              </button>
            </>
          )}
        </div>
      )}
      {runForm && v.state === "PUBLISHED" && (
        <form className="editor" onSubmit={launch}>
          <label>
            Run input (JSON)
            <textarea name="input" defaultValue="{}" required />
          </label>
          <button className="primary" disabled={action.busy}>
            Submit run
          </button>
        </form>
      )}
      {action.error}
    </article>
  );
}

export function AgentDetail({ session: s, id }: { session: Session; id: string }) {
  const path = "/agents/" + id,
    q = useResource<Agent>(s, path),
    versions = usePages<Version>(s, path + "/versions"),
    [creating, setCreating] = useState(false);
  return (
    <>
      <a className="back" href="/agents">
        Agent registry
      </a>
      <Heading title={q.data?.name ?? "Agent configuration"}>
        {canWrite(s) && q.data && (
          <button className="primary" onClick={() => setCreating(!creating)}>
            <Plus size={16} />
            New version
          </button>
        )}
      </Heading>
      <ResultState query={q} />
      {q.data && <p className="context-line">{q.data.description || q.data.id}</p>}
      {creating && (
        <VersionForm
          session={s}
          path={path + "/versions"}
          done={() => {
            setCreating(false);
            void versions.refetch();
          }}
        />
      )}
      <Panel title="Version history">
        <ResultState query={versions} />
        {versions.data &&
          (!versions.data.pages[0].items.length ? (
            <p className="empty">No versions yet.</p>
          ) : (
            versions.data.pages
              .flatMap((p) => p.items)
              .map((v) => (
                <VersionCard
                  key={v.id}
                  s={s}
                  v={v}
                  path={path + "/versions"}
                  refresh={() => void versions.refetch()}
                />
              ))
          ))}
        <More query={versions} />
      </Panel>
    </>
  );
}
