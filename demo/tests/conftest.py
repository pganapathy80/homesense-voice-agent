"""Dummy LiveKit credentials so the suite runs on a fresh clone.

A couple of tests construct LiveKit Inference components, whose constructors
require LIVEKIT_API_KEY/SECRET to be set even though the tests never touch the
network. Real values from .env.local (loaded by agent.py) take precedence.
"""

import os

os.environ.setdefault("LIVEKIT_URL", "wss://placeholder.livekit.cloud")
os.environ.setdefault("LIVEKIT_API_KEY", "test-api-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-api-secret")
