"""Small, cancellation-safe per-session locks for upstream setup only."""
import asyncio
from contextlib import asynccontextmanager


class SessionLocks:
    def __init__(self):
        self.entries = {}

    @asynccontextmanager
    async def hold(self, key):
        entry = self.entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.entries.pop(key, None)
