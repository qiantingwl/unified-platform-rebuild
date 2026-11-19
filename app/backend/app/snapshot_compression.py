"""Compress allowlisted console reads and static assets; all API streams bypass it."""
from starlette.middleware.gzip import GZipMiddleware


def accepts_gzip(headers):
    qualities = {}
    for key, value in headers:
        if key.lower() != b"accept-encoding":
            continue
        for item in value.decode("latin-1").split(","):
            coding, *parameters = item.strip().lower().split(";")
            quality = 1.0
            for parameter in parameters:
                name, _, raw = parameter.strip().partition("=")
                if name == "q":
                    try:
                        quality = float(raw)
                    except ValueError:
                        quality = 0.0
            qualities[coding.strip()] = quality if 0 <= quality <= 1 else 0.0
    return qualities.get("gzip", qualities.get("*", 0)) > 0


CONSOLE_READS = frozenset({
    "/api/snapshot", "/api/proxies", "/api/proxy-pools", "/api/proxy-subscriptions",
    "/api/sms/channels", "/api/catalogs", "/api/dispatch-policies",
    "/api/registration", "/api/registration-batches", "/api/jobs",
})


class SnapshotGZipMiddleware:
    def __init__(self, app, minimum_size=1024):
        self.app = app
        # The pinned Starlette implementation offloads large compression chunks
        # to a worker thread. Level 1 favors latency over maximum compression.
        self.compressed = GZipMiddleware(app, minimum_size=minimum_size, compresslevel=1)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        allowed = path in CONSOLE_READS or path.startswith("/assets/")
        if (scope["type"] != "http" or not allowed or scope.get("method", "GET") != "GET"
                or not accepts_gzip(scope.get("headers", []))):
            await self.app(scope, receive, send)
            return
        # Starlette uses a substring check. Normalize only after respecting
        # q=0, case and wildcard negotiation; never alter non-snapshot traffic.
        normalized = {**scope, "headers": [
            (key, value) for key, value in scope.get("headers", [])
            if key.lower() != b"accept-encoding"
        ] + [(b"accept-encoding", b"gzip")]}
        await self.compressed(normalized, receive, send)
