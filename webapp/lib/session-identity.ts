/**
 * Mock "signed-in customer" state for the HomeSense demo.
 *
 * Stands in for a real authenticated session. The account it resolves is sent
 * to /api/token, which stamps it onto the participant so the voice agent can
 * greet the caller by name instead of asking them to verify.
 *
 * Resolution order:
 *   1. ?account= / ?phone= in the URL  (explicit override, always wins)
 *   2. localStorage                    (the sign-in pill)
 *   3. signed in as the demo account   (so the happy path needs no setup)
 */

export const DEMO_ACCOUNT = 'HS-10042';
export const DEMO_DISPLAY_NAME = 'Prash G.';

const STORAGE_KEY = 'homesense.signedIn';
const SIGNED_OUT = 'signed-out';

export type SessionIdentity = {
  /** Account to send to the token endpoint, or null when signed out. */
  account: string | null;
  /** Name to show in the pill. */
  displayName: string;
  /** True when the account came from a ?account= / ?phone= URL override. */
  fromUrl: boolean;
};

function urlOverride(): { account?: string; phone?: string } {
  if (typeof window === 'undefined') return {};
  const params = new URLSearchParams(window.location.search);
  return {
    account: params.get('account')?.trim() || undefined,
    phone: params.get('phone')?.trim() || undefined,
  };
}

export function getSessionIdentity(): SessionIdentity {
  const { account, phone } = urlOverride();
  if (account || phone) {
    return { account: account ?? null, displayName: account ?? phone ?? '', fromUrl: true };
  }

  // No window during server rendering: assume the default so the first paint
  // matches what the client resolves, avoiding a hydration flash.
  const stored = typeof window === 'undefined' ? null : window.localStorage.getItem(STORAGE_KEY);
  if (stored === SIGNED_OUT) {
    return { account: null, displayName: '', fromUrl: false };
  }
  return { account: DEMO_ACCOUNT, displayName: DEMO_DISPLAY_NAME, fromUrl: false };
}

export function setSignedIn(signedIn: boolean): void {
  if (typeof window === 'undefined') return;
  if (signedIn) {
    window.localStorage.removeItem(STORAGE_KEY);
  } else {
    window.localStorage.setItem(STORAGE_KEY, SIGNED_OUT);
  }
}

/**
 * Query string for /api/token. Preserves any params already on the page so an
 * explicit ?account= / ?phone= override keeps working unchanged.
 */
export function tokenQuery(): string {
  if (typeof window === 'undefined') return '';

  const params = new URLSearchParams(window.location.search);
  if (!params.get('account') && !params.get('phone')) {
    const { account } = getSessionIdentity();
    if (account) params.set('account', account);
  }

  const query = params.toString();
  return query ? `?${query}` : '';
}
