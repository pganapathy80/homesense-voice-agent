'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleNav, LiveBadge } from '@/components/app/console-nav';
import {
  EDGES,
  NODES,
  NODE_LABELS,
  type NodeId,
  TOOL_NODES,
  TYPE,
  VIEW,
  describeFlowEvent,
  edgesForEvent,
  nodesForEvent,
} from './flow-model';

const POLL_MS = 1000;
const PULSE_MS = 1200;
const TICKER_LENGTH = 6;

type Event = Record<string, unknown> & { ts?: number; kind?: string };
type Flight = { id: number; edge: string; reverse?: boolean };

let flightId = 0;

/**
 * A labelled box.
 *
 * Text sits at fixed offsets inside the box in user units, so a label can never
 * drift onto its neighbour. Sub-captions carry `flow-caption`, which is hidden
 * by CSS on narrow viewports where the diagram scales down too far for them to
 * be legible — losing them is better than rendering them as mush.
 */
function Node({
  id,
  title,
  subtitle,
  metric,
  active,
  tone = 'default',
}: {
  id: NodeId;
  title: string;
  subtitle?: string;
  metric?: string;
  active: boolean;
  tone?: 'default' | 'container' | 'inner';
}) {
  const box = NODES[id];
  const fill =
    tone === 'container' ? 'rgba(255,255,255,0.02)' : tone === 'inner' ? '#1c1c1c' : '#171717';

  // Vertical rhythm: with a subtitle the title sits high and the caption below
  // it; without one the title is centred.
  const titleY = tone === 'container' ? box.y + 32 : subtitle ? box.y + 40 : box.y + box.h / 2 + 9;
  const subtitleY = box.y + 68;

  // A node that shows a metric puts its text in two fixed columns — title hard
  // left, metric hard right — instead of centring the title. Centred text grows
  // in both directions, so a four-digit latency ("1047 ms") walked left into the
  // title and collided. Two anchored columns cannot converge no matter how many
  // digits the number has.
  const hasMetric = Boolean(metric);
  const titleX = tone === 'container' || hasMetric ? box.x + 16 : box.x + box.w / 2;
  const titleAnchor = tone === 'container' || hasMetric ? 'start' : 'middle';
  const subtitleX = hasMetric ? box.x + 16 : box.x + box.w / 2;
  const subtitleAnchor = hasMetric ? 'start' : 'middle';

  return (
    <g className={active ? 'flow-pulse' : undefined}>
      <rect
        x={box.x}
        y={box.y}
        width={box.w}
        height={box.h}
        rx={tone === 'container' ? 16 : 10}
        fill={fill}
        stroke={active ? '#2dd4bf' : 'rgba(255,255,255,0.16)'}
        strokeWidth={active ? 2.5 : 1.25}
        strokeDasharray={tone === 'container' ? '7 6' : undefined}
      />
      <text
        x={tone === 'container' ? box.x + 18 : titleX}
        y={titleY}
        textAnchor={titleAnchor}
        fill="#ffffff"
        fontSize={tone === 'container' ? TYPE.containerTitle : TYPE.title}
        fontWeight={600}
      >
        {title}
      </text>
      {subtitle && (
        <text
          className="flow-caption"
          x={subtitleX}
          y={subtitleY}
          textAnchor={subtitleAnchor}
          fontSize={TYPE.subtitle}
          fill="#a3a3a3"
        >
          {subtitle}
        </text>
      )}
      {metric && (
        <text
          x={box.x + box.w - 16}
          y={box.y + 40}
          textAnchor="end"
          fontSize={TYPE.metric}
          fontFamily="var(--font-commit-mono), monospace"
          fill="#5eead4"
        >
          {metric}
        </text>
      )}
    </g>
  );
}

export default function FlowPage() {
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pulses, setPulses] = useState<Set<string>>(new Set());
  const [flights, setFlights] = useState<Flight[]>([]);
  const [latestTurn, setLatestTurn] = useState<Event | null>(null);
  const [currentTool, setCurrentTool] = useState<Event | null>(null);
  const [fetchedAt, setFetchedAt] = useState<string>('');
  const since = useRef(0);

  /** Light up nodes and send dots down edges for one event. */
  const play = useCallback((event: Event) => {
    const kind = String(event.kind ?? '');
    const target = event.target == null ? undefined : String(event.target);

    const nodes = nodesForEvent(kind, target);
    if (nodes.length) {
      setPulses((prev) => new Set([...prev, ...nodes]));
      setTimeout(
        () => setPulses((prev) => new Set([...prev].filter((n) => !nodes.includes(n as NodeId)))),
        PULSE_MS
      );
    }

    edgesForEvent(kind, target).forEach((hop, i) => {
      // Stagger so a multi-hop path reads as travel, not a flash.
      setTimeout(() => {
        const flight = { id: ++flightId, edge: hop.edge, reverse: hop.reverse };
        setFlights((prev) => [...prev, flight]);
        setTimeout(() => setFlights((prev) => prev.filter((f) => f.id !== flight.id)), 900);
      }, i * 260);
    });

    if (kind === 'turn') setLatestTurn(event);
    if (kind === 'tool_call') setCurrentTool(event);
  }, []);

  const poll = useCallback(async () => {
    try {
      const res = await fetch(`/api/flow?since=${since.current}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { events: Event[]; latest: number; fetchedAt: string };
      setError(null);
      setFetchedAt(new Date(data.fetchedAt).toLocaleTimeString());

      if (data.events.length) {
        const first = since.current === 0;
        since.current = data.latest || since.current;
        setEvents((prev) => [...prev, ...data.events].slice(-60));

        if (first) {
          // Don't replay the whole backlog as animation — but DO adopt the
          // latest readings from it, or a reload mid-call would show a diagram
          // with no per-stage numbers until the next turn happened to land.
          const lastOf = (kind: string) =>
            [...data.events].reverse().find((e) => e.kind === kind) ?? null;
          const turn = lastOf('turn');
          const tool = lastOf('tool_call');
          if (turn) setLatestTurn(turn);
          if (tool) setCurrentTool(tool);
        } else {
          data.events.forEach(play);
        }
      } else if (data.latest) {
        since.current = data.latest;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'unreachable');
    }
  }, [play]);

  useEffect(() => {
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [poll]);

  const on = (id: string) => pulses.has(id);
  const ms = (key: string) =>
    latestTurn && typeof latestTurn[key] === 'number'
      ? `${Math.round(latestTurn[key] as number)} ms`
      : undefined;

  const ticker = [...events].reverse().slice(0, TICKER_LENGTH);
  const agent = NODES.agent;
  const tools = NODES.tools;

  return (
    // h-svh + overflow-hidden: the diagram takes exactly the space left over
    // between the nav and the ticker and letterboxes into it. Nothing scrolls,
    // so nothing can slide under anything else.
    <main className="bg-background text-foreground flex h-svh flex-col overflow-hidden md:pt-20">
      <div className="shrink-0">
        <ConsoleNav status={<LiveBadge error={error} at={fetchedAt} />} />
      </div>

      <div className="relative min-h-0 flex-1 p-2">
        {events.length === 0 && !error && (
          <p className="text-muted-foreground absolute inset-0 z-10 flex items-center justify-center text-lg">
            Waiting for a call…
          </p>
        )}

        <svg
          viewBox={`0 0 ${VIEW.w} ${VIEW.h}`}
          preserveAspectRatio="xMidYMid meet"
          className={`h-full w-full ${events.length === 0 ? 'opacity-25' : ''}`}
          role="img"
          aria-label="Live call flow through the LiveKit voice agent"
        >
          {/* Edges first, so nodes sit on top of them. */}
          {Object.entries(EDGES).map(([id, d]) => (
            <path
              key={id}
              d={d}
              fill="none"
              stroke="rgba(255,255,255,0.12)"
              strokeWidth={2}
              strokeDasharray={id.includes('human') ? '7 6' : undefined}
            />
          ))}

          {/* Containers */}
          <Node id="agent" title="Agent worker" active={on('agent')} tone="container" />
          <text
            className="flow-caption"
            x={agent.x + 18}
            y={agent.y + 68}
            fontSize={TYPE.caption}
            fill="#737373"
          >
            one process per call
          </text>
          <text
            x={agent.x + agent.w / 2}
            y={agent.y + agent.h - 20}
            textAnchor="middle"
            fontSize={TYPE.caption}
            fill="rgba(45,212,191,0.8)"
          >
            via LiveKit Inference
          </text>

          <Node id="tools" title="Tools" active={on('tools')} tone="container" />

          {/* Pipeline */}
          <Node
            id="stt"
            title="STT"
            subtitle="deepgram/nova-3"
            metric={ms('stt_ms')}
            active={on('stt')}
            tone="inner"
          />
          <Node
            id="llm"
            title="LLM"
            subtitle="openai/gpt-4.1-mini"
            metric={ms('llm_ttft_ms')}
            active={on('llm')}
            tone="inner"
          />
          <Node
            id="tts"
            title="TTS"
            subtitle="cartesia/sonic-2"
            metric={ms('tts_ttfb_ms')}
            active={on('tts')}
            tone="inner"
          />

          {/* Endpoints */}
          <Node id="caller" title="Caller" subtitle="phone · web" active={on('caller')} />
          <Node
            id="cloud"
            title="LiveKit Cloud"
            subtitle="SIP · Room · Dispatch"
            active={on('cloud')}
          />
          <Node
            id="human"
            title="Human Agent Desk"
            subtitle="warm transfer + summary"
            active={on('human')}
          />

          {TOOL_NODES.map((tool) => (
            <Node key={tool} id={tool} title={NODE_LABELS[tool]} active={on(tool)} tone="inner" />
          ))}

          {/* Current tool call, in the gap reserved between Tools and Human. */}
          <text
            x={tools.x + tools.w / 2}
            y={tools.y + tools.h + 30}
            textAnchor="middle"
            fontSize={TYPE.caption}
            fill="#5eead4"
          >
            {currentTool
              ? `${String(currentTool.tool ?? '')}${
                  currentTool.summary ? ` · ${String(currentTool.summary).slice(0, 34)}` : ''
                }`
              : ''}
          </text>

          {/* Travelling dots */}
          {flights.map((flight) => (
            <circle key={flight.id} r={8} fill="#2dd4bf">
              <animateMotion
                dur="0.85s"
                fill="freeze"
                path={EDGES[flight.edge]}
                keyPoints={flight.reverse ? '1;0' : '0;1'}
                keyTimes="0;1"
                calcMode="linear"
              />
            </circle>
          ))}
        </svg>
      </div>

      {/* Fixed height so the diagram's share of the viewport never changes as
          events arrive and the ticker fills up. */}
      <footer className="border-foreground/10 h-[6.75rem] shrink-0 overflow-hidden border-t px-6 py-2">
        <h2 className="text-muted-foreground mb-1 font-mono text-[0.7rem] tracking-wider uppercase">
          Recent activity
        </h2>
        {ticker.length === 0 ? (
          <p className="text-muted-foreground text-sm">Nothing yet.</p>
        ) : (
          <ol className="grid grid-cols-1 gap-x-8 text-sm leading-6 sm:grid-cols-2 lg:grid-cols-3">
            {ticker.map((event, i) => (
              <li key={`${event.ts}-${i}`} className="truncate">
                <span className="text-muted-foreground mr-2 font-mono text-xs">
                  {typeof event.ts === 'number'
                    ? new Date(event.ts * 1000).toLocaleTimeString()
                    : ''}
                </span>
                <span className={i === 0 ? 'text-teal-300' : 'text-foreground'}>
                  {describeFlowEvent(event)}
                </span>
              </li>
            ))}
          </ol>
        )}
      </footer>
    </main>
  );
}
