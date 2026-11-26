import { type ReactNode, useState } from "react";
import { Inbox, RefreshCw } from "lucide-react";
import { short, time, type Run } from "./api";

export function Heading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="heading">
      <h1>{title}</h1>
      {children}
    </div>
  );
}
export function Panel({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}
export function Status({ value }: { value: string }) {
  return (
    <span className={"state state-" + value.toLowerCase()}>{value.replaceAll("_", " ").toLowerCase()}</span>
  );
}
type Result = { isPending: boolean; error: Error | null; refetch: () => unknown };
export function ResultState({ query }: { query: Result }) {
  if (query.error)
    return (
      <div className="feedback" role="alert">
        {query.error.message}{" "}
        <button type="button" className="refresh" onClick={() => void query.refetch()}>
          <RefreshCw size={14} /> Retry
        </button>
      </div>
    );
  return query.isPending ? (
    <p className="feedback" role="status">
      Loading…
    </p>
  ) : null;
}
export function More({
  query,
}: {
  query: { hasNextPage: boolean; isFetchingNextPage: boolean; fetchNextPage: () => unknown };
}) {
  return query.hasNextPage ? (
    <button
      type="button"
      className="refresh more"
      disabled={query.isFetchingNextPage}
      onClick={() => void query.fetchNextPage()}
    >
      Load more
    </button>
  ) : null;
}
export function Table({
  headers,
  rows,
  empty,
}: {
  headers: string[];
  rows: { key: string; cells: ReactNode[] }[];
  empty: string;
}) {
  if (!rows.length)
    return (
      <p className="empty">
        <Inbox size={19} />
        {empty}
      </p>
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h} scope="col">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              {row.cells.map((cell, i) => (
                <td key={headers[i]}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function RunTable({ runs }: { runs: Run[] }) {
  return (
    <Table
      headers={["Execution", "State", "Version ID", "Started"]}
      empty="No runs match this view."
      rows={runs.map((r) => ({
        key: r.id,
        cells: [
          <a className="mono" href={"/runs/" + r.id} title={r.id}>
            {short(r.id)}
          </a>,
          <Status value={r.status} />,
          <code title={r.agent_version_id}>{short(r.agent_version_id)}</code>,
          time(r.created_at),
        ],
      }))}
    />
  );
}
export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
export function useAction() {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function perform(task: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await task();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The operation failed.");
    } finally {
      setBusy(false);
    }
  }
  return {
    busy,
    perform,
    error: error ? (
      <p role="alert" className="feedback">
        {error}
      </p>
    ) : null,
  };
}
