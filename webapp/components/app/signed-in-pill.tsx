'use client';

import { useEffect, useState } from 'react';
import {
  DEMO_ACCOUNT,
  DEMO_DISPLAY_NAME,
  getSessionIdentity,
  setSignedIn,
} from '@/lib/session-identity';

/**
 * Mock sign-in state for the demo.
 *
 * Signed in, the agent greets the caller by name. Signed out, it has to verify
 * them — which is the identity gate, and the more interesting half to show.
 */
export function SignedInPill() {
  // Start from the server-rendered assumption, then reconcile after mount so
  // localStorage and URL params are read without a hydration mismatch.
  const [identity, setIdentity] = useState({
    account: DEMO_ACCOUNT as string | null,
    displayName: DEMO_DISPLAY_NAME,
    fromUrl: false,
  });

  useEffect(() => {
    setIdentity(getSessionIdentity());
  }, []);

  const toggle = (next: boolean) => {
    setSignedIn(next);
    // Reload so the token source is rebuilt with the new identity. The call has
    // not started yet at this point, so nothing is interrupted.
    const url = new URL(window.location.href);
    url.searchParams.delete('account');
    url.searchParams.delete('phone');
    window.location.replace(url.toString());
  };

  const signedIn = identity.account !== null;

  return (
    <div className="border-border bg-muted/50 flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs">
      <span
        aria-hidden
        className={`size-2 rounded-full ${signedIn ? 'bg-teal-500' : 'bg-muted-foreground/40'}`}
      />
      {signedIn ? (
        <>
          <span className="text-foreground font-medium">
            Signed in as {identity.displayName || identity.account}
          </span>
          <span className="text-muted-foreground">·</span>
          <span className="text-muted-foreground font-mono">{identity.account}</span>
          <button
            type="button"
            onClick={() => toggle(false)}
            className="ml-1 text-teal-600 underline-offset-2 hover:underline dark:text-teal-400"
          >
            Sign out
          </button>
        </>
      ) : (
        <>
          <span className="text-muted-foreground">Not signed in</span>
          <button
            type="button"
            onClick={() => toggle(true)}
            className="ml-1 text-teal-600 underline-offset-2 hover:underline dark:text-teal-400"
          >
            Sign in
          </button>
        </>
      )}
    </div>
  );
}
