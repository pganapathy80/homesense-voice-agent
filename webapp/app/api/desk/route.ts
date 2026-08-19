import { NextResponse } from 'next/server';
import { readCrmLog, readHandoffs, readTickets } from '@/lib/demo-data';

/**
 * Feed for the Human Agent Desk.
 *
 * Reads the same append-only JSONL files the voice agent writes, so the desk
 * shows exactly what the backend recorded — no second source of truth. In a
 * real deployment these would be the ticketing and CRM systems' own APIs; the
 * shape the UI consumes would not change.
 */

const MAX_ROWS = 50;

export const revalidate = 0;
export const dynamic = 'force-dynamic';

export async function GET() {
  const [handoffs, tickets, crm] = await Promise.all([readHandoffs(), readTickets(), readCrmLog()]);

  return NextResponse.json(
    {
      handoffs: handoffs.slice(-MAX_ROWS),
      tickets: tickets.slice(-MAX_ROWS),
      crm: crm.slice(-MAX_ROWS),
      fetchedAt: new Date().toISOString(),
    },
    { headers: { 'Cache-Control': 'no-store' } }
  );
}
