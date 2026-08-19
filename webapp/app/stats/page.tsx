'use client';

import { useCallback, useEffect, useState } from 'react';
import { ConsoleNav, LiveBadge } from '@/components/app/console-nav';

const POLL_MS = 2000;

// Categorical slots 1–4 from the validated dark palette (validate_palette.js:
// all checks pass on the #171717 surface; worst adjacent CVD ΔE 8.4).
const STAGE_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500'];
const STAGES = [
  { key: 'stt', label: 'STT' },
  { key: 'eou', label: 'End of turn' },
  { key: 'llm', label: 'LLM first token' },
  { key: 'tts', label: 'TTS first byte' },
] as const;

type Stats = {
  calls: { total: number; today: number; active: number; completed: number };
  latency: {
    avgE2e: number | null;
    p95E2e: number | null;
    stt: number | null;
    eou: number | null;
    llm: number | null;
    tts: number | null;
    turns: number;
    recent: number[];
  };
  outcomes: {
    containmentRate: number | null;
    transferRate: number | null;
    transfers: number;
    reasons: Record<string, number>;
    ticketsOpened: number;
    interruptions: number;
    avgHandleSeconds: number | null;
  };
  knowledge: { lookups: number; hitRate: number | null };
  identifiedVia: Record<string, number>;
  cost: {
    todaysTalkMinutes: number;
    agent: number;
    human: number;
    agentPerMin: number;
    humanPerMin: number;
  };
  fetchedAt?: string;
};

const EMPTY: Stats = {
  calls: { total: 0, today: 0, active: 0, completed: 0 },
  latency: {
    avgE2e: null,
    p95E2e: null,
    stt: null,
    eou: null,
    llm: null,
    tts: null,
    turns: 0,
    recent: [],
  },
  outcomes: {
    containmentRate: null,
    transferRate: null,
    transfers: 0,
    reasons: {},
    ticketsOpened: 0,
    interruptions: 0,
    avgHandleSeconds: null,
  },
  knowledge: { lookups: 0, hitRate: null },
  identifiedVia: {},
  cost: { todaysTalkMinutes: 0, agent: 0, human: 0, agentPerMin: 0.07, humanPerMin: 0.75 },
};

const ms = (v: number | null) => (v === null ? '—' : `${Math.round(v)}`);
const pct = (v: number | null) => (v === null ? '—' : `${Math.round(v * 100)}`);

const VIA_LABELS: Record<string, string> = {
  caller_id: 'Caller ID',
  web_session: 'Web session',
  account_number: 'Account number',
  phone_number: 'Phone number',
};

function Tile({
  label,
  value,
  unit,
  hint,
  children,
}: {
  label: string;
  value?: string;
  unit?: string;
  hint?: string;
  children?: React.ReactNode;
}) {
  return (
    <section className="border-foreground/15 bg-card flex flex-col rounded-xl border p-5">
      <h2 className="text-muted-foreground text-base font-medium">{label}</h2>
      {value !== undefined && (
        <p className="text-foreground mt-2 flex items-baseline gap-1.5 text-[2.75rem] leading-none font-semibold tracking-tight tabular-nums">
          {value}
          {unit && <span className="text-muted-foreground text-lg font-normal">{unit}</span>}
        </p>
      )}
      {children}
      {hint && <p className="text-muted-foreground mt-2 text-sm">{hint}</p>}
    </section>
  );
}

/** Latency over the last 30 turns. One series, so no legend — the title names it. */
function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) {
    return <p className="text-muted-foreground mt-3 text-sm">Not enough turns yet.</p>;
  }
  const w = 320;
  const h = 64;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (i: number) => (i / (values.length - 1)) * w;
  const y = (v: number) => h - ((v - min) / span) * (h - 8) - 4;
  const line = values
    .map((v, i) => `${i === 0 ? 'M' : 'L'} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`)
    .join(' ');

  return (
    <figure className="mt-3">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="h-16 w-full"
        role="img"
        aria-label={`Voice-to-voice latency over the last ${values.length} turns`}
      >
        <path
          d={line}
          fill="none"
          stroke="#2dd4bf"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <circle cx={x(values.length - 1)} cy={y(values[values.length - 1])} r={4} fill="#2dd4bf" />
      </svg>
      <figcaption className="text-muted-foreground mt-1 flex justify-between font-mono text-xs">
        <span>{Math.round(min)} ms low</span>
        <span>last {values.length} turns</span>
        <span>{Math.round(max)} ms high</span>
      </figcaption>
    </figure>
  );
}

/** Where the voice-to-voice budget goes — parts of one whole, so a stacked bar. */
function LatencyBreakdown({ latency }: { latency: Stats['latency'] }) {
  const parts = STAGES.map((s, i) => ({
    ...s,
    value: latency[s.key] ?? 0,
    color: STAGE_COLORS[i],
  }));
  const total = parts.reduce((a, p) => a + p.value, 0);

  if (total === 0) {
    return <p className="text-muted-foreground mt-2 text-sm">Waiting for turns.</p>;
  }

  return (
    <div className="mt-3">
      <div className="flex h-7 w-full gap-[2px] overflow-hidden rounded">
        {parts.map((p) => (
          <div
            key={p.key}
            style={{ width: `${(p.value / total) * 100}%`, background: p.color }}
            className="first:rounded-l last:rounded-r"
            title={`${p.label} ${Math.round(p.value)} ms`}
          />
        ))}
      </div>
      <ul className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
        {parts.map((p) => (
          <li key={p.key} className="flex items-center gap-2">
            <span
              aria-hidden
              className="size-2.5 shrink-0 rounded-sm"
              style={{ background: p.color }}
            />
            <span className="text-muted-foreground">{p.label}</span>
            <span className="text-foreground ml-auto font-mono tabular-nums">
              {Math.round(p.value)} ms
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Ranked counts. Identity comes from the labels, so one colour is correct here. */
function Breakdown({
  data,
  labels,
}: {
  data: Record<string, number>;
  labels?: Record<string, string>;
}) {
  const rows = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (rows.length === 0) return <p className="text-muted-foreground mt-2 text-sm">None yet.</p>;
  const max = Math.max(...rows.map(([, n]) => n));

  return (
    <ul className="mt-3 space-y-2">
      {rows.map(([key, count]) => (
        <li key={key} className="text-sm">
          <div className="flex justify-between gap-2">
            <span className="text-foreground">{labels?.[key] ?? key}</span>
            <span className="text-muted-foreground font-mono tabular-nums">{count}</span>
          </div>
          <div className="bg-muted mt-1 h-1.5 w-full overflow-hidden rounded-full">
            <div
              className="h-full rounded-full bg-teal-500"
              style={{ width: `${(count / max) * 100}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function StatsPage() {
  const [stats, setStats] = useState<Stats>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [seen, setSeen] = useState(false);

  const poll = useCallback(async () => {
    try {
      const res = await fetch('/api/stats', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStats((await res.json()) as Stats);
      setSeen(true);
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

  const { calls, latency, outcomes, knowledge, identifiedVia, cost } = stats;
  const noCalls = seen && calls.total === 0;

  return (
    <main className="bg-background text-foreground min-h-svh md:pt-20">
      <ConsoleNav
        status={
          <LiveBadge
            error={error}
            at={stats.fetchedAt ? new Date(stats.fetchedAt).toLocaleTimeString() : ''}
          />
        }
      />

      {noCalls && (
        <p className="text-muted-foreground px-6 pt-4 text-base">
          Waiting for calls — every tile below is live and will fill in as the assistant works.
        </p>
      )}

      <div className="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label="Calls today" value={String(calls.today)} hint={`${calls.total} all time`} />
        <Tile
          label="Active now"
          value={String(calls.active)}
          hint={`${calls.completed} completed`}
        />
        <Tile
          label="Avg voice-to-voice"
          value={ms(latency.avgE2e)}
          unit="ms"
          hint={`${latency.turns} turns measured`}
        />
        <Tile
          label="p95 voice-to-voice"
          value={ms(latency.p95E2e)}
          unit="ms"
          hint="95% of turns are faster"
        />

        <Tile
          label="Containment rate"
          value={pct(outcomes.containmentRate)}
          unit="%"
          hint="Calls resolved without a human"
        />
        <Tile
          label="Transfer rate"
          value={pct(outcomes.transferRate)}
          unit="%"
          hint={`${outcomes.transfers} transferred`}
        />
        <Tile label="Tickets opened" value={String(outcomes.ticketsOpened)} />
        <Tile
          label="Avg handle time"
          value={
            outcomes.avgHandleSeconds === null
              ? '—'
              : Math.round(outcomes.avgHandleSeconds).toString()
          }
          unit="s"
          hint={`${outcomes.interruptions} interruptions`}
        />

        <div className="sm:col-span-2">
          <Tile label="Latency over the last 30 turns">
            <Sparkline values={latency.recent} />
          </Tile>
        </div>
        <div className="sm:col-span-2">
          <Tile label="Where the time goes" hint="Average per turn, summing to voice-to-voice">
            <LatencyBreakdown latency={latency} />
          </Tile>
        </div>

        <Tile label="Escalation reasons">
          <Breakdown data={outcomes.reasons} />
        </Tile>
        <Tile label="Identified via">
          <Breakdown data={identifiedVia} labels={VIA_LABELS} />
        </Tile>
        <Tile
          label="Knowledge base"
          value={String(knowledge.lookups)}
          hint={`lookups · ${pct(knowledge.hitRate)}% found an article`}
        />
        <Tile
          label="Estimated cost today"
          value={`$${cost.agent.toFixed(2)}`}
          hint={`vs $${cost.human.toFixed(2)} for a human · ${cost.todaysTalkMinutes.toFixed(1)} talk-minutes`}
        >
          <p className="text-muted-foreground mt-2 text-xs">
            Computed from LiveKit list prices (${cost.agentPerMin.toFixed(2)}/min) against an
            assumed ${cost.humanPerMin.toFixed(2)}/min loaded human agent cost.
          </p>
        </Tile>
      </div>
    </main>
  );
}
