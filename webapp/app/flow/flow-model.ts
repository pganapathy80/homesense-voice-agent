/**
 * Geometry for the live architecture diagram.
 *
 * Everything here is in SVG user units against a fixed viewBox. The page
 * renders it with preserveAspectRatio="xMidYMid meet", so the whole diagram
 * scales uniformly (letterboxes) and NEVER reflows — which means label
 * collisions are impossible to introduce by resizing the window. If two labels
 * don't collide here, they don't collide at any viewport.
 *
 * Boxes are sized against the longest label they must hold; see LABEL_FIT in
 * the tests-by-inspection note at the bottom.
 */

export const VIEW = { w: 1280, h: 600 };

/** Type scale, in user units. */
export const TYPE = {
  title: 26,
  subtitle: 18,
  metric: 20,
  containerTitle: 24,
  caption: 19,
} as const;

export type NodeId =
  | 'caller'
  | 'cloud'
  | 'agent'
  | 'stt'
  | 'llm'
  | 'tts'
  | 'tools'
  | 'db'
  | 'crm'
  | 'kb'
  | 'ticketing'
  | 'mcp'
  | 'human';

export type Box = { x: number; y: number; w: number; h: number };

export const NODES: Record<NodeId, Box> = {
  caller: { x: 20, y: 248, w: 196, h: 112 },
  cloud: { x: 244, y: 224, w: 236, h: 160 },
  agent: { x: 512, y: 68, w: 300, h: 472 },
  stt: { x: 536, y: 150, w: 252, h: 92 },
  llm: { x: 536, y: 262, w: 252, h: 92 },
  tts: { x: 536, y: 374, w: 252, h: 92 },
  tools: { x: 850, y: 38, w: 326, h: 382 },
  db: { x: 872, y: 98, w: 282, h: 56 },
  crm: { x: 872, y: 162, w: 282, h: 56 },
  kb: { x: 872, y: 226, w: 282, h: 56 },
  ticketing: { x: 872, y: 290, w: 282, h: 56 },
  mcp: { x: 872, y: 354, w: 282, h: 56 },
  human: { x: 850, y: 468, w: 326, h: 100 },
};

export const TOOL_NODES: NodeId[] = ['db', 'crm', 'kb', 'ticketing', 'mcp'];

export const NODE_LABELS: Record<string, string> = {
  db: 'Account DB',
  crm: 'CRM',
  kb: 'Knowledge Base',
  ticketing: 'Ticketing',
  mcp: 'MCP server',
  human: 'Human Agent Desk',
};

const cy = (b: Box) => b.y + b.h / 2;
const cx = (b: Box) => b.x + b.w / 2;

/** Straight edge between the facing sides of two boxes. */
function edge(from: NodeId, to: NodeId): string {
  const a = NODES[from];
  const b = NODES[to];
  if (b.x >= a.x + a.w) return `M ${a.x + a.w} ${cy(a)} L ${b.x} ${cy(b)}`;
  if (a.x >= b.x + b.w) return `M ${a.x} ${cy(a)} L ${b.x + b.w} ${cy(b)}`;
  return `M ${cx(a)} ${a.y + a.h} L ${cx(b)} ${b.y}`;
}

const LLM = NODES.llm;
const TTS = NODES.tts;
const CALLER = NODES.caller;
const HUMAN = NODES.human;

export const EDGES: Record<string, string> = {
  'caller-cloud': edge('caller', 'cloud'),
  'cloud-stt': `M ${NODES.cloud.x + NODES.cloud.w} ${cy(NODES.cloud)} L ${NODES.stt.x} ${cy(NODES.stt)}`,
  'stt-llm': `M ${cx(NODES.stt)} ${NODES.stt.y + NODES.stt.h} L ${cx(LLM)} ${LLM.y}`,
  'llm-tts': `M ${cx(LLM)} ${LLM.y + LLM.h} L ${cx(TTS)} ${TTS.y}`,
  // The reply travels back to the caller beneath the whole diagram.
  'tts-caller': `M ${TTS.x} ${cy(TTS)} L ${NODES.agent.x - 18} ${cy(TTS)} L ${NODES.agent.x - 18} 578 L ${cx(CALLER)} 578 L ${cx(CALLER)} ${CALLER.y + CALLER.h}`,
  'llm-human': `M ${cx(LLM)} ${LLM.y + LLM.h} L ${cx(LLM)} 556 L ${HUMAN.x - 16} 556 L ${HUMAN.x - 16} ${cy(HUMAN)} L ${HUMAN.x} ${cy(HUMAN)}`,
  ...Object.fromEntries(
    TOOL_NODES.flatMap((tool) => [
      [`llm-${tool}`, edge('llm', tool)],
      [`${tool}-llm`, edge('llm', tool)],
    ])
  ),
};

/** Which edges light up, in order, for a given event kind. */
export function edgesForEvent(
  kind: string,
  target?: string
): { edge: string; reverse?: boolean }[] {
  switch (kind) {
    case 'call_started':
      return [{ edge: 'caller-cloud' }];
    case 'user_turn':
      return [{ edge: 'caller-cloud' }, { edge: 'cloud-stt' }, { edge: 'stt-llm' }];
    case 'tool_call': {
      if (target === 'human') return [{ edge: 'llm-human' }];
      const tool = target && TOOL_NODES.includes(target as NodeId) ? target : 'db';
      return [{ edge: `llm-${tool}` }, { edge: `${tool}-llm`, reverse: true }];
    }
    case 'turn':
    case 'agent_speech':
      return [{ edge: 'llm-tts' }, { edge: 'tts-caller' }];
    case 'transfer':
    case 'escalation':
      return [{ edge: 'llm-human' }];
    default:
      return [];
  }
}

/** Which nodes pulse for a given event kind. */
export function nodesForEvent(kind: string, target?: string): NodeId[] {
  switch (kind) {
    case 'call_started':
      return ['caller', 'cloud'];
    case 'user_turn':
      return ['caller', 'stt'];
    case 'tool_call':
      return target === 'human'
        ? ['llm', 'human']
        : ['llm', 'tools', (TOOL_NODES.includes(target as NodeId) ? target : 'db') as NodeId];
    case 'turn':
      return ['llm', 'tts', 'caller'];
    case 'agent_speech':
      return ['tts', 'caller'];
    case 'handoff':
      return ['agent'];
    case 'transfer':
    case 'escalation':
      return ['llm', 'human'];
    case 'interruption':
      return ['caller', 'stt'];
    case 'session_end':
      return ['caller'];
    default:
      return [];
  }
}

/** One-line plain-English description for the ticker. */
export function describeFlowEvent(event: Record<string, unknown>): string {
  const kind = String(event.kind ?? '');
  const str = (k: string) => (event[k] == null ? '' : String(event[k]));
  const ms = (k: string) => (typeof event[k] === 'number' ? Math.round(event[k] as number) : null);

  switch (kind) {
    case 'call_started':
      return `Call started · ${str('channel')}${str('customer') ? ` · ${str('customer')}` : ''}`;
    case 'user_turn':
      return `Caller: ${str('text')}`;
    case 'agent_speech':
      return `Agent: ${str('text')}`;
    case 'turn':
      return `TURN e2e ${ms('e2e_ms')} ms · llm ${ms('llm_ttft_ms')} · tts ${ms('tts_ttfb_ms')}`;
    case 'tool_call': {
      const label = NODE_LABELS[str('target')] ?? str('target');
      return `${label} → ${str('tool')}${str('summary') ? ` · ${str('summary')}` : ''}`;
    }
    case 'handoff':
      return `Handoff ${str('from')} → ${str('to')}`;
    case 'escalation':
      return `Escalation · reason ${str('reason')}`;
    case 'transfer':
      return `Warm transfer → human${str('ticket_id') ? ` · ${str('ticket_id')}` : ''}`;
    case 'interruption':
      return 'Caller interrupted the agent';
    case 'session_end':
      return `Call ended · ${str('turns')} turns${event.transferred ? ' · transferred' : ''}`;
    default:
      return kind;
  }
}
