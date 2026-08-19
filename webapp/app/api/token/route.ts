import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { RoomConfiguration } from '@livekit/protocol';

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

// NOTE: you are expected to define the following environment variables in `.env.local`:
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;

// don't cache the results
export const revalidate = 0;

export async function POST(req: Request) {
  // make an exception for the vercel preview environment
  if (process.env.NODE_ENV !== 'development' && process.env.IS_VERCEL_PREVIEW !== 'true') {
    throw new Error(
      'THIS API ROUTE IS INSECURE. DO NOT USE THIS ROUTE IN PRODUCTION WITHOUT AN AUTHENTICATION LAYER.'
    );
  }

  try {
    if (LIVEKIT_URL === undefined) {
      throw new Error('LIVEKIT_URL is not defined');
    }
    if (API_KEY === undefined) {
      throw new Error('LIVEKIT_API_KEY is not defined');
    }
    if (API_SECRET === undefined) {
      throw new Error('LIVEKIT_API_SECRET is not defined');
    }

    // Authenticated-web identity: the page forwards its own query string here,
    // so /?account=HS-10042 arrives as ?account=HS-10042 on this request. We
    // put it on the participant token so the agent can greet the caller by
    // name without asking for an account number. Absent -> unchanged behaviour.
    //
    // NOTE: this is demo-grade. The value is whatever the URL said, so it is a
    // claim about identity, not proof of it. A real deployment would derive
    // this from the signed-in session server-side and never from a query param.
    const url = new URL(req.url);
    const account = url.searchParams.get('account')?.trim() || undefined;
    const phone = url.searchParams.get('phone')?.trim() || undefined;

    const sessionIdentity: Record<string, string> = {};
    if (account) sessionIdentity.account = account;
    if (phone) sessionIdentity.phone = phone;
    const isAuthenticated = Object.keys(sessionIdentity).length > 0;

    // Metadata and attributes both, because the agent reads either one.
    const attributes: Record<string, string> | undefined = isAuthenticated
      ? {
          ...(account ? { 'homesense.account': account } : {}),
          ...(phone ? { 'homesense.phone': phone } : {}),
        }
      : undefined;

    // Parse room config from request body.
    const body = await req.json();
    const roomConfig = body?.room_config
      ? RoomConfiguration.fromJson(body.room_config, { ignoreUnknownFields: true })
      : new RoomConfiguration();

    // Generate participant token
    const participantName = 'user';
    const participantIdentity = `voice_assistant_user_${Math.floor(Math.random() * 10_000)}`;
    const roomName = `voice_assistant_room_${Math.floor(Math.random() * 10_000)}`;

    const participantToken = await createParticipantToken(
      {
        identity: participantIdentity,
        name: participantName,
        ...(isAuthenticated ? { metadata: JSON.stringify(sessionIdentity) } : {}),
        ...(attributes ? { attributes } : {}),
      },
      roomName,
      roomConfig
    );

    // Return connection details
    const data: ConnectionDetails = {
      serverUrl: LIVEKIT_URL,
      roomName,
      participantName,
      participantToken,
    };
    const headers = new Headers({
      'Cache-Control': 'no-store',
    });
    return NextResponse.json(data, { headers });
  } catch (error) {
    if (error instanceof Error) {
      console.error(error);
      return new NextResponse(error.message, { status: 500 });
    }
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  roomConfig: RoomConfiguration | undefined
): Promise<string> {
  const at = new AccessToken(API_KEY, API_SECRET, {
    ...userInfo,
    ttl: '15m',
  });
  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
  };
  at.addGrant(grant);

  if (roomConfig) {
    at.roomConfig = roomConfig;
  }

  return at.toJwt();
}
