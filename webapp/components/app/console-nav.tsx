'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const LINKS = [
  { href: '/desk', label: 'Agent Desk' },
  { href: '/flow', label: 'Live Flow' },
  { href: '/stats', label: 'Metrics' },
];

/**
 * Shared nav for the three operator screens. Deliberately absent from the
 * customer-facing call page, which stays exactly as it was.
 */
export function ConsoleNav({ status }: { status?: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <header className="border-foreground/10 flex flex-wrap items-center justify-between gap-3 border-b px-6 py-3">
      <div className="flex items-center gap-5">
        <span className="text-foreground text-lg font-semibold tracking-tight">HomeSense</span>
        <nav className="flex items-center gap-1">
          {LINKS.map((link) => {
            const active = pathname === link.href;
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded-full px-3 py-1.5 text-sm transition-colors ${
                  active
                    ? 'bg-teal-500/15 font-medium text-teal-300'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
      </div>
      {status}
    </header>
  );
}

/** Live/stale indicator used by all three screens. */
export function LiveBadge({ error, at }: { error?: string | null; at?: string }) {
  return (
    <div className="border-border bg-muted/50 flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs">
      <span
        aria-hidden
        className={`size-2 rounded-full ${error ? 'bg-red-500' : 'animate-pulse bg-teal-500'}`}
      />
      <span className="text-foreground font-medium">{error ? 'Feed unavailable' : 'Live'}</span>
      <span className="text-muted-foreground">·</span>
      <span className="text-muted-foreground font-mono">{error ?? at ?? ''}</span>
    </div>
  );
}
