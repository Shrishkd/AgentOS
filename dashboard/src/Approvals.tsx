import { useState, type FormEvent } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ClipboardCheck, LogOut, RefreshCw, X } from "lucide-react";

type Session = { tenant: string; token: string; key: string };
type Proposal = {
  id: string; run_id: string; step_id: string; tool_version_id: string; tool_name: string;
  arguments: Record<string, unknown>; risk: string; status: string; action_digest: string; expires_at: string;
};
type Page = { items: Proposal[]; next_cursor: string | null };

async function request<T>(session: Session, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/v1/tenants/${session.tenant}/approvals${path}`, {
    ...init, cache: "no-store", headers: { Authorization: `Bearer ${session.token}`, "Content-Type": "application/json" },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? "Could not reach the approval service.");
  return data as T;
}

function Inbox({ session, disconnect }: { session: Session; disconnect: () => void }) {
  const [selected, select] = useState<Proposal | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const query = useInfiniteQuery({
    queryKey: ["approval-inbox", session.key], initialPageParam: "",
    queryFn: ({ pageParam, signal }) => request<Page>(session, "?status=pending&limit=25" +
      (pageParam ? "&cursor=" + encodeURIComponent(pageParam) : ""), { signal }),
    getNextPageParam: page => page.next_cursor ?? undefined,
    refetchInterval: 5000, retry: false,
  });
  const rows = query.data?.pages.flatMap(page => page.items) ?? [];
  async function decide(decision: "approve" | "reject") {
    if (!selected || busy) return;
    setBusy(true); setNotice("");
    try {
      const receipt = await request<Proposal>(session, `/${selected.id}/decision`, { method: "POST",
        body: JSON.stringify({ decision, action_digest: selected.action_digest, reason: reason.trim() || null }) });
      setNotice(`${receipt.tool_name} ${receipt.status}.`);
      select(null); setReason("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Decision failed.");
    } finally {
      setBusy(false); void query.refetch();
    }
  }
  const current = rows.some(row => row.id === selected?.id && row.action_digest === selected?.action_digest);
  return <>
    <div className="heading approval-heading"><h1>Approvals</h1><button className="refresh" onClick={disconnect} disabled={busy}><LogOut size={16}/> Disconnect</button></div>
    <p className="tenant-label">Tenant <code>{session.tenant}</code></p>
    {notice && <p role="status" className="decision-notice">{notice}</p>}
    <div className="inbox">
      <section className="panel" aria-label="Pending approvals">
        <div className="panel-head approval-heading"><h2>Pending review</h2><button className="refresh" aria-label="Refresh approvals" onClick={() => void query.refetch()}><RefreshCw size={16}/></button></div>
        {query.error && <p role="alert" className="inbox-message">{query.error.message}</p>}
        {query.isPending && <p className="inbox-message">Loading approvals…</p>}
        {query.isSuccess && !rows.length && <p className="inbox-message">No actions are waiting for approval.</p>}
        {rows.map(row => <button key={row.id} className="proposal" aria-pressed={selected?.id === row.id} disabled={busy}
          onClick={() => { select(row); setNotice(""); setReason(""); }}>
          <span><strong>{row.tool_name}</strong><small>Run {row.run_id.slice(0, 8)}</small></span><span className="risk">{row.risk} risk</span>
        </button>)}
        {query.hasNextPage && <button className="refresh more" onClick={() => void query.fetchNextPage()}>Load more</button>}
      </section>
      {selected && <section className="panel review" aria-label="Review action">
        <h2>Review {selected.tool_name}</h2>
        <dl><dt>Run</dt><dd>{selected.run_id}</dd><dt>Tool version ID</dt><dd>{selected.tool_version_id}</dd>
          <dt>Expires</dt><dd>{new Date(selected.expires_at).toLocaleString()}</dd>
          <dt>Action digest</dt><dd>{selected.action_digest}</dd></dl>
        <h3>Proposed arguments</h3><pre>{JSON.stringify(selected.arguments, null, 2)}</pre>
        <label>Decision reason (optional)<textarea maxLength={1000} value={reason} onChange={e => setReason(e.target.value)} /></label>
        {!current && <p role="status">This action has left the pending queue.</p>}
        <div className="actions"><button className="primary" disabled={busy || !current} onClick={() => void decide("approve")}><Check size={16}/> Approve</button>
          <button className="refresh" disabled={busy || !current} onClick={() => void decide("reject")}><X size={16}/> Reject</button></div>
      </section>}
    </div>
  </>;
}

export default function Approvals() {
  const [session, setSession] = useState<Session | null>(null);
  const cache = useQueryClient();
  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setSession({ tenant: String(data.get("tenant")).trim(), token: String(data.get("token")).trim(), key: crypto.randomUUID() });
    form.reset();
  }
  if (session) return <Inbox key={session.key} session={session} disconnect={() => {
    cache.removeQueries({ queryKey: ["approval-inbox", session.key] }); setSession(null);
  }}/>;
  return <><h1>Approvals</h1><form className="panel connect" onSubmit={connect}>
    <ClipboardCheck size={24}/><h2>Open a tenant inbox</h2>
    <label>Tenant ID<input name="tenant" required pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}" autoComplete="off" /></label>
    <label>Access token<input name="token" type="password" required autoComplete="off" /></label>
    <button className="primary" type="submit">Open inbox</button>
  </form></>;
}
