import { promises as fs } from 'node:fs';
import path from 'node:path';

/**
 * Reads the append-only JSONL files the voice agent writes in demo/data/.
 *
 * Lives here rather than in a route file because Next.js route modules may only
 * export request handlers and a fixed set of config keys — exporting a helper
 * from one breaks the generated route types.
 */

export type DataRow = Record<string, unknown> & { ts?: number; kind?: string };

/** demo/data, resolved from the webapp directory. Override with DEMO_DATA_DIR. */
export function dataDir(): string {
  return process.env.DEMO_DATA_DIR ?? path.join(process.cwd(), '..', 'demo', 'data');
}

/**
 * Parse a JSONL file into rows, oldest first.
 *
 * Tolerates two normal states: the file not existing yet (nothing has happened
 * on this machine), and a final line that is only partially flushed (a call is
 * in flight right now).
 */
export async function readJsonl(name: string): Promise<DataRow[]> {
  let raw: string;
  try {
    raw = await fs.readFile(path.join(dataDir(), name), 'utf8');
  } catch {
    return [];
  }

  const rows: DataRow[] = [];
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object') rows.push(parsed as DataRow);
    } catch {
      // Half-written last line; it will be complete on the next poll.
    }
  }
  return rows;
}

export const readEvents = () => readJsonl('events.jsonl');
export const readTickets = () => readJsonl('tickets.jsonl');
export const readHandoffs = () => readJsonl('handoffs.jsonl');
export const readCrmLog = () => readJsonl('crm_log.jsonl');
