export type Handoff = {
  handoff_id?: string;
  created_at?: string;
  account_number?: string | null;
  customer_name?: string | null;
  reason?: string;
  summary?: string;
  mode?: string;
  ticket_id?: string | null;
  session_id?: string | null;
  transcript?: { role?: string; text?: string }[];
};

export type Ticket = {
  ticket_id?: string;
  created_at?: string;
  account_number?: string;
  customer_name?: string;
  issue_summary?: string;
  device_id?: string | null;
  priority?: string;
  status?: string;
};

export type CrmEvent = {
  logged_at?: string;
  event_type?: string;
  account_number?: string | null;
  tool?: string;
  result?: string;
  role?: string;
  text?: string;
  note?: string;
  ticket_id?: string;
  titles?: string[];
  query?: string;
  input?: string;
  order_id?: string;
  device_id?: string;
  identified_via?: string;
  escalation_reason?: string;
  channel?: string;
  session_id?: string;
};

export type DeskFeed = {
  handoffs: Handoff[];
  tickets: Ticket[];
  crm: CrmEvent[];
  fetchedAt?: string;
};

/** The four escalation reasons the agent can emit, plus how to colour them. */
export const REASON_STYLES: Record<string, string> = {
  irate: 'bg-red-500/15 text-red-400 ring-1 ring-red-500/30',
  policy: 'bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30',
  unresolved: 'bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30',
  requested: 'bg-teal-500/15 text-teal-300 ring-1 ring-teal-500/30',
};

/** `reason` arrives as "irate" or "irate: threatened to cancel". */
export function splitReason(reason?: string): { code: string; detail: string } {
  if (!reason) return { code: 'unknown', detail: '' };
  const [head, ...rest] = reason.split(':');
  return { code: head.trim().toLowerCase(), detail: rest.join(':').trim() };
}

/**
 * The AI summary is written as labelled lines (CALLER:, ASKED:, SENTIMENT: …).
 * Split it into pairs so the desk can render it as a table instead of a wall
 * of text; anything unlabelled falls through as a plain paragraph.
 */
export function parseSummary(summary?: string): {
  fields: { label: string; value: string }[];
  rest: string;
} {
  const fields: { label: string; value: string }[] = [];
  const loose: string[] = [];

  for (const line of (summary ?? '').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const match = /^([A-Z][A-Z ]{2,20}):\s*(.+)$/.exec(trimmed);
    if (match) {
      fields.push({ label: match[1].trim(), value: match[2].trim() });
    } else {
      loose.push(trimmed);
    }
  }
  return { fields, rest: loose.join(' ') };
}

export function shortTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** One-line description of a CRM event for the activity feed. */
export function describeEvent(event: CrmEvent): string {
  switch (event.event_type) {
    case 'tool_call':
      return [
        event.tool,
        event.result && event.result !== 'ok' ? `(${event.result})` : '',
        event.ticket_id ?? event.order_id ?? event.device_id ?? '',
        event.titles?.length ? event.titles.join(', ') : '',
        event.query ? `“${event.query}”` : '',
      ]
        .filter(Boolean)
        .join(' ');
    case 'transcript':
      return `${event.role === 'user' ? 'Caller' : 'Agent'}: ${event.text ?? ''}`;
    case 'account_note':
      return `Note: ${event.note ?? ''}`;
    case 'call_started':
      return `Call started (${event.channel ?? 'web'})`;
    case 'identified':
      return `Identified via ${event.identified_via ?? 'unknown'}`;
    case 'call_ended':
      return `Call ended${event.escalation_reason ? ` — escalated (${event.escalation_reason})` : ''}`;
    default:
      return event.event_type ?? '';
  }
}
