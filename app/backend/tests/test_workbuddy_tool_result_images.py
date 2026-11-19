from copy import deepcopy

import pytest

from app.adapters import WorkBuddy, WorkBuddyInternational
from app.protocols import normalize


@pytest.mark.parametrize("adapter", [WorkBuddy, WorkBuddyInternational])
def test_messages_tool_result_images_reach_user_without_text_or_image_loss(adapter):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8=" * 300}}
    raw = {"model": "synthetic", "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "read-image", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-image", "content": [{"type": "text", "text": "exact tool output"}, image]}]},
    ]}
    before = deepcopy(raw)
    normalized = normalize(raw, "messages")
    normalized_before = deepcopy(normalized)
    outgoing = adapter.outbound(normalized)
    assert raw == before and normalized == normalized_before
    messages = outgoing["messages"]
    assert [m["role"] for m in messages] == ["assistant", "tool", "user"]
    assert messages[1]["tool_call_id"] == "read-image"
    assert messages[1]["content"] == [{"type": "text", "text": "exact tool output"}]
    assert messages[2]["content"] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + image["source"]["data"]}}]


def test_parallel_tool_results_are_not_split_by_promoted_image_message():
    image = {"type": "image_url", "image_url": {"url": "https://example.test/a.png", "detail": "high"}, "cache_control": {"type": "ephemeral"}}
    second_image = {"type": "image_url", "image_url": {"url": "https://example.test/b.png"}}
    body = {"messages": [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": [image]},
        {"role": "tool", "tool_call_id": "b", "content": [{"type": "text", "text": "second exact output"}, second_image]},
        {"role": "user", "content": "original follow-up"},
    ]}
    outgoing = WorkBuddy.outbound(body)["messages"]
    assert [m["role"] for m in outgoing] == ["assistant", "tool", "tool", "user", "user"]
    assert outgoing[1]["content"] == ""
    assert outgoing[2]["content"] == [{"type": "text", "text": "second exact output"}]
    assert outgoing[3]["content"] == [image, second_image]
    assert outgoing[4]["content"] == "original follow-up"
    assert body["messages"][1]["content"] == [image]


def test_text_only_tools_and_user_images_remain_in_place():
    body = {"messages": [
        {"role": "tool", "tool_call_id": "a", "content": [{"type": "text", "text": "literal"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]},
    ]}
    assert WorkBuddy.outbound(body)["messages"] == body["messages"]


def test_common_protocol_does_not_promote_images_for_other_platforms():
    body = {"model": "synthetic", "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": [{"type": "image", "source": {"type": "url", "url": "https://example.test/a.png"}}]}]},
    ]}
    normalized = normalize(body, "messages")
    assert len(normalized["messages"]) == 1
    assert normalized["messages"][0]["role"] == "tool"
    assert normalized["messages"][0]["content"][0]["type"] == "image_url"
