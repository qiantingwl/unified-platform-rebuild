"""WPS first-session coordination without holding database locks over HTTP."""
import asyncio
import time
import logging

import anyio

from .upstream import UpstreamError


def fresh(value):
    return (isinstance(value, dict) and isinstance(value.get('session_id'), str)
            and isinstance(value.get('run_id'), str)
            and type(value.get('created')) in (int, float)
            and time.time() - value['created'] < 3600)


async def resolve(adapter, key, cache_id, create):
    """Called under the process-local per-key setup lock.

    The enclosing Gateway also bounds all setup by its one startup deadline.
    Legacy get/put cache implementations keep working without claim methods.
    """
    shared = getattr(adapter, 'shared_cache', None)
    cached = await asyncio.to_thread(shared.get, cache_id) if shared else adapter.cache.get(key)
    if fresh(cached):
        return cached
    coordinated = shared is not None and all(callable(getattr(shared, name, None)) for name in ('claim', 'publish', 'release'))
    token = None
    try:
        if coordinated:
            deadline = time.monotonic() + 90
            while True:
                pending = asyncio.create_task(asyncio.to_thread(shared.claim, cache_id, ttl_seconds=90))
                try:
                    token = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    # The DB thread may commit after its await is cancelled.
                    # Join it before cleanup so an abandoned claim is released.
                    with anyio.CancelScope(shield=True):
                        token = await pending
                    raise
                # A prior owner may have published just before our successful
                # claim. Re-read while owning the new lease before any HTTP.
                cached = await asyncio.to_thread(shared.get, cache_id)
                if fresh(cached):
                    return cached
                if token:
                    break
                if time.monotonic() >= deadline:
                    raise UpstreamError('upstream_queue_wait', 503, True)
                await asyncio.sleep(min(.2, max(0, deadline-time.monotonic())))
        cached = await create()
        if coordinated:
            pending = asyncio.create_task(asyncio.to_thread(shared.publish, cache_id, token, cached, ttl_seconds=3600))
            try:
                published = await asyncio.shield(pending)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await pending
                raise
            if not published:
                # Never use a session produced by an expired/replaced owner.
                raise UpstreamError('request_lease_lost', 503)
            token = None  # publish atomically saved the value and released it
        elif shared:
            await asyncio.to_thread(shared.put, cache_id, cached, ttl_seconds=3600)
        if len(adapter.cache) >= 10000:
            adapter.cache.pop(min(adapter.cache, key=lambda k: adapter.cache[k]['created']))
        adapter.cache[key] = cached
        return cached
    finally:
        if coordinated and token:
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.to_thread(shared.release, cache_id, token)
                except Exception:
                    # The lease is still bounded by its TTL. Do not replace a
                    # known cancellation/upstream result with cleanup failure.
                    logging.getLogger(__name__).warning('upstream session claim cleanup could not be persisted')
