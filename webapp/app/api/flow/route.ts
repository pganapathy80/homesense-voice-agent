import { NextResponse } from 'next/server';
import { readEvents } from '@/lib/demo-data';

/** Live agent event stream for the flow visualiser. */

const MAX_EVENTS = 200;

export const revalidate = 0;
export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
  const since = Number(new URL(request.url).searchParams.get('since') ?? '0');
  const all = await readEvents();

  // `since` is the ts of the newest event the client already holds.
  const fresh = Number.isFinite(since) && since > 0 ? all.filter((e) => (e.ts ?? 0) > since) : all;

  return NextResponse.json(
    {
      events: fresh.slice(-MAX_EVENTS),
      latest: all.at(-1)?.ts ?? 0,
      fetchedAt: new Date().toISOString(),
    },
    { headers: { 'Cache-Control': 'no-store' } }
  );
}
