import { useState } from "react";
import { Check, RefreshCw, X } from "lucide-react";
import { request, usePages, time, short, type Session } from "./api";
import { Heading, More, ResultState, Status, Table } from "./ui";
type Proposal = {
  id: string;
  run_id: string;
  step_id: string;
  tool_version_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk: string;
  status: string;
  action_digest: string;
  expires_at: string;
  decided_at: string | null;
  reason: string | null;
};
export default function Approvals({ session }: { session: Session }) {
  const [selected, select] = useState<Proposal | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [filter, setFilter] = useState("pending");
  const query = usePages<Proposal>(session, "/approvals" + (filter ? "?status=" + filter : ""), 5000);
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  async function decide(decision: "approve" | "reject") {
    if (!selected || busy) return;
    setBusy(true);
    setNotice("");
    try {
      const receipt = await request<Proposal>(session, `/approvals/${selected.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          action_digest: selected.action_digest,
          reason: reason.trim() || null,
        }),
      });
      setNotice(`${receipt.tool_name} ${receipt.status}.`);
      select(null);
      setReason("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Decision failed.");
    } finally {
      setBusy(false);
      void query.refetch();
    }
  }
  const current = rows.some(
    (row) => row.id === selected?.id && row.action_digest === selected?.action_digest,
  );
  return (
    <>
      <Heading title="Approvals">
        <label className="inline-label">
          State
          <select
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              select(null);
              setNotice("");
            }}
          >
            {["pending", "approved", "rejected", "expired", "cancelled", ""].map((s) => (
              <option value={s} key={s}>
                {s || "All actions"}
              </option>
            ))}
          </select>
        </label>
      </Heading>
      {notice && (
        <p role="status" className="decision-notice">
          {notice}
        </p>
      )}
      {filter !== "pending" ? (
        <section className="panel">
          <ResultState query={query} />
          {query.data && (
            <Table
              headers={["Tool", "Run", "Decision", "Decided", "Reason"]}
              empty="No approval history matches this state."
              rows={rows.map((a) => ({
                key: a.id,
                cells: [
                  <details>
                    <summary>{a.tool_name}</summary>
                    <p>{a.risk} risk</p>
                    <pre className="json">{JSON.stringify(a.arguments, null, 2)}</pre>
                  </details>,
                  <a href={"/runs/" + a.run_id}>{short(a.run_id)}</a>,
                  <Status value={a.status} />,
                  a.decided_at ? time(a.decided_at) : "—",
                  a.reason ?? "—",
                ],
              }))}
            />
          )}
          <More query={query} />
        </section>
      ) : (
        <div className="inbox">
          <section className="panel" aria-label="Pending approvals">
            <div className="panel-head approval-heading">
              <h2>Pending review</h2>
              <button className="refresh" aria-label="Refresh approvals" onClick={() => void query.refetch()}>
                <RefreshCw size={16} />
              </button>
            </div>
            {query.error && (
              <p role="alert" className="inbox-message">
                {query.error.message}
              </p>
            )}
            {query.isPending && <p className="inbox-message">Loading approvals…</p>}
            {query.isSuccess && !rows.length && (
              <p className="inbox-message">No actions are waiting for approval.</p>
            )}
            {rows.map((row) => (
              <button
                key={row.id}
                className="proposal"
                aria-pressed={selected?.id === row.id}
                disabled={busy}
                onClick={() => {
                  select(row);
                  setNotice("");
                  setReason("");
                }}
              >
                <span>
                  <strong>{row.tool_name}</strong>
                  <small>Run {row.run_id.slice(0, 8)}</small>
                </span>
                <span className="risk">{row.risk} risk</span>
              </button>
            ))}
            {query.hasNextPage && (
              <button className="refresh more" onClick={() => void query.fetchNextPage()}>
                Load more
              </button>
            )}
          </section>
          {selected && (
            <section className="panel review" aria-label="Review action">
              <h2>Review {selected.tool_name}</h2>
              <dl>
                <dt>Run</dt>
                <dd>{selected.run_id}</dd>
                <dt>Tool version ID</dt>
                <dd>{selected.tool_version_id}</dd>
                <dt>Expires</dt>
                <dd>{new Date(selected.expires_at).toLocaleString()}</dd>
                <dt>Action digest</dt>
                <dd>{selected.action_digest}</dd>
              </dl>
              <h3>Proposed arguments</h3>
              <pre>{JSON.stringify(selected.arguments, null, 2)}</pre>
              <label>
                Decision reason (optional)
                <textarea maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
              </label>
              {!current && <p role="status">This action has left the pending queue.</p>}
              <div className="actions">
                <button
                  className="primary"
                  disabled={busy || !current || !["approver", "admin"].includes(session.workspace.role)}
                  onClick={() => void decide("approve")}
                >
                  <Check size={16} /> Approve
                </button>
                <button
                  className="refresh"
                  disabled={busy || !current || !["approver", "admin"].includes(session.workspace.role)}
                  onClick={() => void decide("reject")}
                >
                  <X size={16} /> Reject
                </button>
              </div>
            </section>
          )}
        </div>
      )}
    </>
  );
}
