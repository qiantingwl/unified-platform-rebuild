import asyncio
import copy

from app.adapters import WorkBuddy, WorkBuddyInternational
from app.upstream import classify


def test_global_chat_identity_matches_official_without_rewriting_history():
    body = {"model": "deepseek-v4.1-flash-sg", "messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 256}
    original = copy.deepcopy(body)
    credential = {"auth": {"accessToken": "synthetic", "domain": "different-domain"}, "account": {"uid": "synthetic-user"}}

    async def run():
        adapter = WorkBuddyInternational(None, credential, "account", 1)
        url, headers, outgoing = await adapter.prepare(body, "stable-session")
        assert url == "https://www.workbuddy.ai/v2/chat/completions"
        assert headers["User-Agent"] == "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0"
        assert headers["X-Domain"] == "www.workbuddy.ai" and headers["X-IDE-Version"] == "5.8.4"
        assert headers["X-Conversation-ID"] == "stable-session"
        assert headers["X-Conversation-Request-ID"] == headers["X-Root-Request-ID"]
        assert headers["X-Conversation-Message-ID"] == headers["X-Request-ID"]
        assert headers["X-Trace-ID"] == headers["X-B3-TraceId"]
        assert headers["traceparent"] == f"00-{headers['X-Trace-ID']}-{headers['X-B3-SpanId']}-01"
        assert outgoing["messages"] == body["messages"] == original["messages"]
        assert body == original and outgoing["max_tokens"] == 256
        # Billing/renewal and CN headers are deliberately unchanged.
        assert adapter.headers()["X-IDE-Version"] == "5.9.3"
        assert WorkBuddy(None, credential, "account", 1).headers()["X-Domain"] == "different-domain"

    asyncio.run(run())


def test_confirmed_workbuddy_restriction_is_distinct_from_generic_403():
    failure = classify(403, 11140)
    assert failure.code == "permission" and failure.retryable
    assert classify(403).code == "unknown_403"
    assert classify(400, 11128).code == "request_rejected"
    assert classify(400, 11140).code == "upstream_rejected"
