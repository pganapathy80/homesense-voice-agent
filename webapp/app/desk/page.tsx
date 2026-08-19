'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleNav, LiveBadge } from '@/components/app/console-nav';
import {
  type CrmEvent,
  type DeskFeed,
  type Handoff,
  REASON_STYLES,
  type Ticket,
  describeEvent,
  parseSummary,
  shortTime,
  splitReason,
} from './desk-types';

const POLL_MS = 2000;

/** Stable key per row so "what is new since last poll" survives re-renders. */
const handoffKey = (h: Handoff, i: number) => h.handoff_id ?? `handoff-${i}`;
const ticketKey = (t: Ticket, i: number) => t.ticket_id ?? `ticket-${i}`;
const crmKey = (e: CrmEvent, i: number) =>
  `${e.logged_at ?? ''}-${e.event_type ?? ''}-${e.tool ?? ''}-${i}`;

/**
 * Tracks which keys appeared since the previous poll, so freshly arrived rows
 * can flash. Without this you see a static list and miss the moment
 * the agent actually did something.
 */
function useNewKeys(keys: string[]) {
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (seen.current === null) {
      // First load is not "new" — everything would flash at once.
      seen.current = new Set(keys);
      return;
    }
    const added = keys.filter((k) => !seen.current!.has(k));
    if (added.length === 0) return;

    added.forEach((k) => seen.current!.add(k));
    setFresh(new Set(added));
    const timer = setTimeout(() => setFresh(new Set()), 2000);
    return () => clearTimeout(timer);
  }, [keys.join('|')]); // eslint-disable-line react-hooks/exhaustive-deps

  return fresh;
}

function Column({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="border-foreground/15 bg-background/40 flex min-h-[16rem] flex-col rounded-xl border md:min-h-0">
      <header className="border-foreground/10 flex items-center justify-between border-b px-4 py-3">
        <h2 className="text-foreground text-base font-semibold tracking-tight">{title}</h2>
        <span className="text-muted-foreground bg-muted/60 rounded-full px-2 py-0.5 font-mono text-xs">
          {count}
        </span>
      </header>
      <div className="flex-1 space-y-3 overflow-y-auto p-3">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-muted-foreground px-2 py-8 text-center text-sm text-balance">{children}</p>
  );
}

function HandoffCard({
  handoff,
  isNew,
  accepted,
  onAccept,
}: {
  handoff: Handoff;
  isNew: boolean;
  accepted: boolean;
  onAccept: () => void;
}) {
  const { code, detail } = splitReason(handoff.reason);
  const { fields, rest } = parseSummary(handoff.summary);
  const badge = REASON_STYLES[code] ?? 'bg-muted text-muted-foreground ring-1 ring-foreground/15';

  return (
    <article
      className={`border-foreground/15 bg-card rounded-lg border p-4 ${isNew ? 'desk-flash' : ''} ${
        accepted ? 'opacity-60' : ''
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-foreground truncate text-base font-semibold">
            {handoff.customer_name ?? 'Unidentified caller'}
          </h3>
          <p className="text-muted-foreground font-mono text-xs">
            {handoff.account_number ?? 'no account'} · {shortTime(handoff.created_at)}
          </p>
        </div>
        <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${badge}`}>
          {code}
        </span>
      </div>

      {detail && <p className="text-muted-foreground mt-2 text-sm">{detail}</p>}

      {(fields.length > 0 || rest) && (
        <dl className="border-foreground/10 mt-3 space-y-1.5 border-t pt-3 text-sm">
          {fields.map((field) => (
            <div key={field.label} className="grid grid-cols-[7.5rem_1fr] gap-2">
              <dt className="text-muted-foreground font-mono text-xs tracking-wide uppercase">
                {field.label}
              </dt>
              <dd className="text-foreground leading-snug">{field.value}</dd>
            </div>
          ))}
          {rest && <p className="text-foreground leading-snug">{rest}</p>}
        </dl>
      )}

      <div className="mt-4 flex items-center justify-between gap-3">
        <span className="text-muted-foreground font-mono text-xs">
          {handoff.ticket_id ? `Ticket ${handoff.ticket_id}` : 'No ticket'}
        </span>
        <button
          type="button"
          onClick={onAccept}
          disabled={accepted}
          className="rounded-full bg-teal-500 px-4 py-1.5 text-xs font-bold tracking-wider text-teal-950 uppercase transition-colors hover:bg-teal-400 disabled:bg-teal-500/30 disabled:text-teal-200"
        >
          {accepted ? 'Accepted' : 'Accept'}
        </button>
      </div>
    </article>
  );
}

export default function DeskPage() {
  const [feed, setFeed] = useState<DeskFeed>({ handoffs: [], tickets: [], crm: [] });
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());

  const poll = useCallback(async () => {
    try {
      const res = await fetch('/api/desk', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setFeed((await res.json()) as DeskFeed);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'unreachable');
    }
  }, []);

  useEffect(() => {
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  // Files are append-only (oldest first); the desk wants newest at the top.
  const handoffs = [...feed.handoffs].reverse();
  const tickets = [...feed.tickets].reverse();
  const crm = [...feed.crm].reverse();

  const newHandoffs = useNewKeys(handoffs.map(handoffKey));
  const newTickets = useNewKeys(tickets.map(ticketKey));
  const newCrm = useNewKeys(crm.map(crmKey));

  return (
    <main className="bg-background text-foreground flex h-svh flex-col text-base md:pt-20">
      <ConsoleNav status={<LiveBadge error={error} at={shortTime(feed.fetchedAt)} />} />

      <div className="grid min-h-0 flex-1 auto-rows-fr grid-cols-1 gap-4 overflow-y-auto p-4 md:auto-rows-auto md:grid-cols-[1.4fr_1fr_1fr] md:overflow-hidden">
        <Column title="Incoming handoffs" count={handoffs.length}>
          {handoffs.length === 0 ? (
            <Empty>No handoffs yet — waiting for the assistant.</Empty>
          ) : (
            handoffs.map((handoff, i) => {
              const key = handoffKey(handoff, i);
              return (
                <HandoffCard
                  key={key}
                  handoff={handoff}
                  isNew={newHandoffs.has(key)}
                  accepted={accepted.has(key)}
                  onAccept={() => setAccepted((prev) => new Set(prev).add(key))}
                />
              );
            })
          )}
        </Column>

        <Column title="Tickets" count={tickets.length}>
          {tickets.length === 0 ? (
            <Empty>No tickets yet.</Empty>
          ) : (
            tickets.map((ticket, i) => {
              const key = ticketKey(ticket, i);
              return (
                <article
                  key={key}
                  className={`border-foreground/15 bg-card rounded-lg border p-3 ${
                    newTickets.has(key) ? 'desk-flash' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm font-semibold text-teal-400">
                      {ticket.ticket_id}
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                        ticket.priority === 'high'
                          ? 'bg-amber-500/15 text-amber-400'
                          : 'bg-muted text-muted-foreground'
                      }`}
                    >
                      {ticket.priority ?? 'normal'}
                    </span>
                  </div>
                  <p className="text-muted-foreground mt-1 font-mono text-xs">
                    {ticket.account_number} · {ticket.status} · {shortTime(ticket.created_at)}
                  </p>
                  <p className="text-foreground mt-2 text-sm leading-snug">
                    {ticket.issue_summary}
                  </p>
                </article>
              );
            })
          )}
        </Column>

        <Column title="CRM activity" count={crm.length}>
          {crm.length === 0 ? (
            <Empty>No activity yet.</Empty>
          ) : (
            crm.map((event, i) => {
              const key = crmKey(event, i);
              return (
                <div
                  key={key}
                  className={`border-foreground/10 rounded-md border px-3 py-2 ${
                    newCrm.has(key) ? 'desk-flash' : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs font-medium text-teal-400">
                      {event.event_type}
                    </span>
                    <span className="text-muted-foreground font-mono text-xs">
                      {shortTime(event.logged_at)}
                    </span>
                  </div>
                  <p className="text-foreground mt-1 line-clamp-3 text-sm leading-snug">
                    {describeEvent(event)}
                  </p>
                  {event.account_number && (
                    <p className="text-muted-foreground mt-0.5 font-mono text-xs">
                      {event.account_number}
                    </p>
                  )}
                </div>
              );
            })
          )}
        </Column>
      </div>
    </main>
  );
}
