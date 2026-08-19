import { NextResponse } from 'next/server';
import { readEvents, readTickets } from '@/lib/demo-data';

/**
 * KPIs for the metrics screen, computed server-side from the same append-only
 * files the agent writes. Kept out of the client so the page stays a renderer.
 */

export const revalidate = 0;
export const dynamic = 'force-dynamic';

// LiveKit list price per participant-minute, and a loaded cost for a human
// agent minute. Both are assumptions, labelled as such in the UI.
const AGENT_COST_PER_MIN = 0.07;
const HUMAN_COST_PER_MIN = 0.75;

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

function percentile(values: number[], p: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[Math.max(0, index)];
}

const mean = (values: number[]): number | null =>
  values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;

function startOfToday(): number {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
}

export async function GET() {
  const [events, tickets] = await Promise.all([readEvents(), readTickets()]);

  const today = startOfToday();
  const of = (kind: string) => events.filter((e) => e.kind === kind);

  const starts = of('call_started');
  const ends = of('session_end');
  const turns = of('turn');

  // A session is "active" when it started but has not ended.
  const endedSessions = new Set(ends.map((e) => String(e.session_id)));
  const activeNow = starts.filter((e) => !endedSessions.has(String(e.session_id))).length;

  const e2e = turns.map((t) => num(t.e2e_ms)).filter((v): v is number => v !== null);

  const stage = (key: string) =>
    mean(turns.map((t) => num(t[key])).filter((v): v is number => v !== null));

  const transferred = ends.filter((e) => e.transferred === true);
  const reasons: Record<string, number> = {};
  for (const end of ends) {
    const reason = end.escalation_reason;
    if (typeof reason === 'string' && reason) reasons[reason] = (reasons[reason] ?? 0) + 1;
  }

  const kbLookups = of('tool_call').filter((e) => e.tool === 'search_kb');
  const kbHits = kbLookups.filter((e) => e.result === 'ok');

  const identifiedVia: Record<string, number> = {};
  for (const start of [...starts, ...ends]) {
    const via = start.identified_via;
    if (typeof via === 'string' && via) identifiedVia[via] = (identifiedVia[via] ?? 0) + 1;
  }

  // Handle time: pair each session_end with its session_start.
  const startTs = new Map<string, number>();
  for (const start of starts) {
    const id = String(start.session_id);
    if (!startTs.has(id) && num(start.ts) !== null) startTs.set(id, start.ts as number);
  }
  const handleTimes: number[] = [];
  for (const end of ends) {
    const began = startTs.get(String(end.session_id));
    const finished = num(end.ts);
    if (began !== undefined && finished !== null && finished > began) {
      handleTimes.push(finished - began);
    }
  }

  const talkMinutes = handleTimes.reduce((a, b) => a + b, 0) / 60;
  const todaysTalkMinutes =
    handleTimes.length && ends.length
      ? ends
          .filter((e) => (num(e.ts) ?? 0) >= today)
          .reduce((total, end) => {
            const began = startTs.get(String(end.session_id));
            const finished = num(end.ts);
            return began !== undefined && finished !== null && finished > began
              ? total + (finished - began) / 60
              : total;
          }, 0)
      : 0;

  return NextResponse.json(
    {
      calls: {
        total: starts.length,
        today: starts.filter((e) => (num(e.ts) ?? 0) >= today).length,
        active: activeNow,
        completed: ends.length,
      },
      latency: {
        avgE2e: mean(e2e),
        p95E2e: percentile(e2e, 95),
        stt: stage('stt_ms'),
        eou: stage('eou_ms'),
        llm: stage('llm_ttft_ms'),
        tts: stage('tts_ttfb_ms'),
        turns: turns.length,
        recent: e2e.slice(-30),
      },
      outcomes: {
        containmentRate: ends.length ? (ends.length - transferred.length) / ends.length : null,
        transferRate: ends.length ? transferred.length / ends.length : null,
        transfers: transferred.length,
        reasons,
        ticketsOpened: tickets.length,
        interruptions: of('interruption').length,
        avgHandleSeconds: mean(handleTimes),
      },
      knowledge: {
        lookups: kbLookups.length,
        hitRate: kbLookups.length ? kbHits.length / kbLookups.length : null,
      },
      identifiedVia,
      cost: {
        talkMinutes,
        todaysTalkMinutes,
        agent: todaysTalkMinutes * AGENT_COST_PER_MIN,
        human: todaysTalkMinutes * HUMAN_COST_PER_MIN,
        agentPerMin: AGENT_COST_PER_MIN,
        humanPerMin: HUMAN_COST_PER_MIN,
      },
      fetchedAt: new Date().toISOString(),
    },
    { headers: { 'Cache-Control': 'no-store' } }
  );
}
