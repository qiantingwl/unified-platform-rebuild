"""Explicit protocol translation. Unsupported input fails before account selection."""
import json
import hashlib
from copy import deepcopy

from .store import DomainError
from .gateway import normalized_delta


def invalid():
    raise DomainError("unsupported_protocol_field", 422)


def content(parts, protocol):
    if isinstance(parts, str) or parts is None:
        return parts
    if not isinstance(parts, list):
        invalid()
    result = []
    for part in parts:
        if not isinstance(part, dict):
            invalid()
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            if not isinstance(part.get("text"), str):
                invalid()
            result.append({"type": "text", "text": part["text"], **({"cache_control": deepcopy(part["cache_control"])} if "cache_control" in part else {})})
        elif kind == "input_image":
            result.append({"type": "image_url", "image_url": {"url": part["image_url"], "detail": part.get("detail", "auto")}})
        elif kind == "image_url":
            result.append(deepcopy(part))
        elif kind == "image":
            source = part["source"]
            url = source.get("url") if source.get("type") == "url" else f"data:{source['media_type']};base64,{source['data']}"
            result.append({"type": "image_url", "image_url": {"url": url}})
        else:
            invalid()
    return result


def _normalize(raw, protocol):
    if not isinstance(raw, dict) or not isinstance(raw.get("model"), str) or not raw["model"] or len(raw["model"]) > 100:
        invalid()
    common = {"model", "stream", "temperature", "top_p", "tools", "tool_choice", "max_tokens", "stop", "metadata", "prompt_cache_key", "prompt_cache_retention", "conversation_id", "parallel_tool_calls"}
    allowed = common | ({"messages", "stream_options", "presence_penalty", "frequency_penalty", "seed", "response_format", "max_completion_tokens", "n", "reasoning_effort"} if protocol == "chat" else {"input", "instructions", "max_output_tokens", "store", "previous_response_id", "reasoning"} if protocol == "responses" else {"messages", "system", "stop_sequences", "thinking"})
    if set(raw) - allowed or raw.get("n", 1) != 1 or raw.get("store") or raw.get("previous_response_id"):
        invalid()
    if not isinstance(raw.get("stream", False), bool):
        invalid()
    body = {k: deepcopy(v) for k, v in raw.items() if k in {"model", "temperature", "top_p", "max_tokens", "stop", "prompt_cache_key", "prompt_cache_retention", "presence_penalty", "frequency_penalty", "seed", "response_format", "parallel_tool_calls", "max_completion_tokens", "reasoning_effort"}}
    if 'prompt_cache_key' in body and (not isinstance(body['prompt_cache_key'], str) or not 0 < len(body['prompt_cache_key']) <= 512):
        invalid()
    if body.get('prompt_cache_retention') not in {None, 'in-memory', '24h'}:
        invalid()
    if 'parallel_tool_calls' in body and type(body['parallel_tool_calls']) is not bool:
        invalid()
    if protocol == 'messages' and 'thinking' in raw:
        thinking = raw['thinking']
        if not isinstance(thinking, dict) or thinking.get('type') not in {'enabled', 'disabled', 'adaptive'}:
            invalid()
        if set(thinking) - {'type', 'budget_tokens', 'display'} or thinking.get('display') not in {None, 'summarized', 'omitted'}:
            invalid()
        budget = thinking.get('budget_tokens')
        if thinking['type'] == 'enabled':
            if type(budget) is not int or budget < 1024 or type(raw.get('max_tokens')) is not int or budget >= raw['max_tokens']:
                invalid()
        elif budget is not None:
            invalid()
        body['thinking'] = deepcopy(thinking)
    if protocol == "responses" and raw.get("reasoning"):
        reasoning = raw["reasoning"]
        if not isinstance(reasoning, dict) or set(reasoning) - {"effort", "summary"}:
            invalid()
        if reasoning.get("summary") not in {None, "auto"}:
            invalid()
        if "effort" in reasoning:
            body["reasoning_effort"] = reasoning["effort"]
    if "reasoning_effort" in body and body["reasoning_effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        invalid()
    if protocol == "chat":
        if "stream_options" in raw:
            opts = raw["stream_options"]
            if not isinstance(opts, dict) or set(opts) - {"include_usage"} or not isinstance(opts.get("include_usage", False), bool):
                invalid()
            body["stream_options"] = deepcopy(opts)
        messages = deepcopy(raw.get("messages", []))
        if not isinstance(messages, list):
            invalid()
        for m in messages:
            if not isinstance(m, dict):
                invalid()
            if m.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
                invalid()
            if isinstance(m.get("content"), list):
                m["content"] = content(m["content"], protocol)
        body.update({k: deepcopy(raw[k]) for k in ("tools", "tool_choice") if k in raw})
    elif protocol == "responses":
        messages = []
        if raw.get("instructions"):
            messages.append({"role": "system", "content": raw["instructions"]})
        inputs = raw.get("input", [])
        if isinstance(inputs, str):
            inputs = [{"role": "user", "content": inputs}]
        for item in inputs:
            kind = item.get("type", "message")
            if kind == "function_call":
                call = {"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}}
                if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif kind == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
            elif kind == "message":
                messages.append({"role": item["role"], "content": content(item.get("content"), protocol)})
            elif kind == "reasoning":
                summary = item.get("summary", [])
                text = "".join(x["text"] for x in summary if x.get("type") == "summary_text")
                if text:
                    messages.append({"role": "assistant", "content": None, "reasoning_content": text})
            else:
                invalid()
        if "max_output_tokens" in raw:
            body["max_tokens"] = raw["max_output_tokens"]
        if "tools" in raw:
            body["tools"] = []
            for tool in raw["tools"]:
                if tool.get("type") != "function":
                    invalid()
                body["tools"].append({"type": "function", "function": {k: v for k, v in tool.items() if k != "type"}})
        if "tool_choice" in raw:
            choice = raw["tool_choice"]
            body["tool_choice"] = choice if isinstance(choice, str) else {"type": "function", "function": {"name": choice["name"]}}
    else:
        messages = []
        if raw.get("system"):
            messages.append({"role": "system", "content": content(raw["system"], protocol)})
        for m in raw.get("messages", []):
            parts = m.get("content", "")
            if isinstance(parts, str):
                messages.append({"role": m["role"], "content": parts})
                continue
            ordinary, calls, thinking, results = [], [], [], []
            for part in parts:
                if part.get("type") == "tool_use":
                    if 'caller' in part and part['caller'] != {'type': 'direct'}:
                        # Hosted tool callers have no equivalent on the
                        # current Chat-only platform contracts.
                        invalid()
                    calls.append({"id": part["id"], "type": "function", "function": {"name": part["name"], "arguments": json.dumps(part["input"], ensure_ascii=False)}, **({'cache_control': deepcopy(part['cache_control'])} if 'cache_control' in part else {})})
                elif part.get("type") == "tool_result":
                    results.append({"role": "tool", "tool_call_id": part["tool_use_id"], "content": content(part.get("content", ""), protocol), **({'cache_control': deepcopy(part['cache_control'])} if 'cache_control' in part else {})})
                elif part.get("type") == "thinking":
                    # Chat upstreams cannot verify another provider's signed
                    # thinking. Reject opaque bindings rather than strip them.
                    if part.get('signature'):
                        invalid()
                    thinking.append(part["thinking"])
                else:
                    ordinary.extend(content([part], protocol))
            messages.extend(results)
            if ordinary or calls or thinking:
                message = {"role": m["role"], "content": ordinary or None}
                if calls:
                    message["tool_calls"] = calls
                if thinking:
                    message["reasoning_content"] = "".join(thinking)
                messages.append(message)
        if "tools" in raw:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}, **({"cache_control": deepcopy(t["cache_control"])} if "cache_control" in t else {})} for t in raw["tools"]]
        choice = raw.get("tool_choice")
        if choice:
            if 'disable_parallel_tool_use' in choice:
                if type(choice['disable_parallel_tool_use']) is not bool:
                    invalid()
                body['parallel_tool_calls'] = not choice['disable_parallel_tool_use']
            body["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}} if choice.get("type") == "tool" else {"any": "required", "auto": "auto", "none": "none"}.get(choice.get("type"))
            if body["tool_choice"] is None:
                invalid()
        if "stop_sequences" in raw:
            body["stop"] = raw["stop_sequences"]
    if not messages or len(messages) > 1000:
        invalid()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            invalid()
        if message.get("content") is not None and not isinstance(message["content"], (str, list)):
            invalid()
    if "tools" in body:
        if not isinstance(body["tools"], list):
            invalid()
        for tool in body["tools"]:
            if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                invalid()
            function = tool["function"]
            if not isinstance(function.get("name"), str) or not function["name"] or not isinstance(function.get("parameters", {}), dict):
                invalid()
    body["messages"], body["stream"] = messages, True
    for key in ("max_tokens", "max_completion_tokens"):
        if key in body and (not isinstance(body[key], int) or isinstance(body[key], bool) or body[key] < 1):
            invalid()
    # Preserve an explicit cache declaration even across the Messages->Chat
    # conversion; the key depends on the stable prefix, never the latest turn.
    def cache_declared(value):
        if isinstance(value, dict):
            return bool(value.get("cache_control")) or any(cache_declared(v) for v in value.values())
        return isinstance(value, list) and any(cache_declared(v) for v in value)
    if protocol == "messages" and "prompt_cache_key" not in body and cache_declared(raw):
        prefix = {"system": raw.get("system"), "tools": raw.get("tools", [])}
        body["prompt_cache_key"] = "prefix:" + hashlib.sha256(json.dumps(prefix, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    # Do not send metadata/conversation IDs to platforms as arbitrary fields.
    return body


def normalize(raw, protocol):
    try:
        return _normalize(raw, protocol)
    except (KeyError, TypeError, AttributeError, ValueError):
        # Malformed input must fail before selection, not crash as a server 500.
        invalid()


def converted(result, protocol, thinking_display=None):
    if protocol == "chat":
        return result
    m = result["choices"][0]["message"]
    usage = result.get("usage", {})
    normalized = translated_usage(usage, protocol)
    if protocol == "messages":
        blocks = []
        if m.get("reasoning_content") and thinking_display != 'omitted':
            blocks.append({"type": "thinking", "thinking": m["reasoning_content"], "signature": ""})
        if m.get("content"):
            blocks.append({"type": "text", "text": m["content"]})
        for tool in m.get("tool_calls", []):
            try:
                args = json.loads(tool["function"]["arguments"])
            except ValueError:
                raise DomainError("invalid_tool_arguments", 502) from None
            blocks.append({"type": "tool_use", "id": tool["id"], "name": tool["function"]["name"], "input": args})
        return {"id": result["id"], "type": "message", "role": "assistant", "model": result["model"], "content": blocks, "stop_reason": {"tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(result["choices"][0]["finish_reason"], "end_turn"), "stop_sequence": None, "usage": normalized}
    output = []
    if m.get("reasoning_content"):
        output.append({"id": "rs_" + result["id"], "type": "reasoning", "summary": [{"type": "summary_text", "text": m["reasoning_content"]}]})
    if m.get("content"):
        output.append({"id": "msg_" + result["id"], "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": m["content"], "annotations": []}]})
    for tool in m.get("tool_calls", []):
        output.append({"id": "fc_" + tool["id"], "type": "function_call", "call_id": tool["id"], "name": tool["function"]["name"], "arguments": tool["function"]["arguments"], "status": "completed"})
    reason = result["choices"][0]["finish_reason"]
    incomplete = {"length": "max_output_tokens", "content_filter": "content_filter"}.get(reason)
    return {"id": result["id"], "object": "response", "created_at": result["created"], "model": result["model"], "status": "incomplete" if incomplete else "completed", "incomplete_details": {"reason": incomplete} if incomplete else None, "output": output, "usage": normalized or None}


def translated_usage(usage, protocol):
    """Translate reported counters; absent provider evidence is not a zero."""
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    prompt = count(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = count(usage.get("completion_tokens", usage.get("output_tokens")))
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = count(details.get("cached_tokens", usage.get("cache_read_input_tokens")))
    created = count(usage.get("cache_creation_input_tokens"))
    result = {}
    if prompt is not None:
        result["input_tokens"] = max(0, prompt-(cached or 0)-(created or 0)) if protocol == "messages" and "prompt_tokens" in usage else prompt
    if completion is not None:
        result["output_tokens"] = completion
    if protocol == "messages":
        if cached is not None:
            result["cache_read_input_tokens"] = cached
        if created is not None:
            result["cache_creation_input_tokens"] = created
        if isinstance(usage.get("cache_creation"), dict):
            result["cache_creation"] = deepcopy(usage["cache_creation"])
    else:
        total = count(usage.get("total_tokens"))
        if total is not None or prompt is not None and completion is not None:
            result["total_tokens"] = total if total is not None else prompt+completion
        if cached is not None:
            result["input_tokens_details"] = {"cached_tokens": cached}
        output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            result["output_tokens_details"] = deepcopy(output_details)
    return result


def sse(data, event=None):
    return (f"event: {event}\n" if event else "") + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


class StreamTranslator:
    """Incremental text/reasoning/tools with deterministic block indices."""
    def __init__(self, protocol, collector, thinking_display=None):
        self.protocol, self.collector = protocol, collector
        self.thinking_display = thinking_display
        self.blocks, self.sequence = {}, 0
        self.message_active = None
        self.message_index = -1

    def event(self, kind, **data):
        if self.protocol == "responses":
            data["sequence_number"] = self.sequence
            self.sequence += 1
        return sse({"type": kind, **data}, kind)

    def start(self):
        c = self.collector
        if self.protocol == "messages":
            return [self.event("message_start", message={"id": c.id, "type": "message", "role": "assistant", "model": c.model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}})]
        return [self.event("response.created", response={"id": c.id, "object": "response", "status": "in_progress", "model": c.model, "output": []})]

    def feed(self, item):
        if "error" in item:
            return [self.event("error", error=item["error"])]
        events = []
        d = normalized_delta(item.get("delta", {}), "call_"+self.collector.id)
        if self.protocol == 'messages':
            # Anthropic blocks are sequential. Parallel Chat tool fragments
            # are accumulated by Collector and emitted contiguously at finish.
            for kind, value in [('thinking', d.get('reasoning_content')), ('text', d.get('content'))]:
                if kind == 'thinking' and self.thinking_display == 'omitted':
                    continue
                if not value:
                    continue
                if self.message_active != kind:
                    if self.message_active is not None:
                        events.append(self.event('content_block_stop', index=self.message_index))
                    self.message_index += 1
                    self.message_active = kind
                    block = {'type': 'thinking', 'thinking': '', 'signature': ''} if kind == 'thinking' else {'type': 'text', 'text': ''}
                    events.append(self.event('content_block_start', index=self.message_index, content_block=block))
                delta = {'type': 'thinking_delta', 'thinking': value} if kind == 'thinking' else {'type': 'text_delta', 'text': value}
                events.append(self.event('content_block_delta', index=self.message_index, delta=delta))
            return events
        values = [("text", d.get("content")), ("thinking", d.get("reasoning_content"))]
        values += [("tool:" + str(t.get("index", 0)), t) for t in d.get("tool_calls", [])]
        for key, value in values:
            if value is None or value == "":
                continue
            tool = key.startswith("tool:")
            if key not in self.blocks:
                index = len(self.blocks)
                identity = value.get("id") if tool else ("msg_" if key == "text" else "rs_") + self.collector.id
                if tool and not identity:
                    # Most providers send id/name before argument fragments.
                    raise DomainError("invalid_stream_frame", 502)
                self.blocks[key] = {"index": index, "id": identity, "text": "", "name": value.get("function", {}).get("name", "") if tool else ""}
                if self.protocol == "messages":
                    block = {"type": "tool_use", "id": identity, "name": self.blocks[key]["name"], "input": {}} if tool else {"type": "thinking", "thinking": ""} if key == "thinking" else {"type": "text", "text": ""}
                    events.append(self.event("content_block_start", index=index, content_block=block))
                else:
                    block = {"id": "fc_" + identity, "type": "function_call", "call_id": identity, "name": self.blocks[key]["name"], "arguments": "", "status": "in_progress"} if tool else {"id": identity, "type": "reasoning", "summary": []} if key == "thinking" else {"id": identity, "type": "message", "role": "assistant", "content": [], "status": "in_progress"}
                    events.append(self.event("response.output_item.added", output_index=index, item=block))
                    if not tool:
                        events.append(self.event("response.reasoning_summary_part.added" if key == "thinking" else "response.content_part.added", item_id=identity, output_index=index, **({"summary_index": 0} if key == "thinking" else {"content_index": 0}), part={"type": "summary_text", "text": ""} if key == "thinking" else {"type": "output_text", "text": "", "annotations": []}))
            b = self.blocks[key]
            if tool and value.get("function", {}).get("name") and b.get("started"):
                b["name"] += value["function"]["name"]
            b["started"] = True
            text = value.get("function", {}).get("arguments", "") if tool else value
            if not item.get('placeholder'):
                b["text"] += text
            if self.protocol == "messages":
                delta = {"type": "input_json_delta", "partial_json": text} if tool else {"type": "thinking_delta", "thinking": text} if key == "thinking" else {"type": "text_delta", "text": text}
                events.append(self.event("content_block_delta", index=b["index"], delta=delta))
            else:
                events.append(self.event("response.function_call_arguments.delta" if tool else "response.reasoning_summary_text.delta" if key == "thinking" else "response.output_text.delta", item_id=("fc_" if tool else "") + b["id"], output_index=b["index"], delta=text, **({} if tool else {"summary_index": 0} if key == "thinking" else {"content_index": 0})))
        return events

    def response_result(self):
        """The final Responses object in actual streamed output-index order."""
        result = converted(self.collector.result(), self.protocol, self.thinking_display)
        if self.protocol != 'responses':
            return result
        output = []
        for key, block in self.blocks.items():
            identity = block['id']
            if key.startswith('tool:'):
                output.append({'id': 'fc_'+identity, 'type': 'function_call', 'call_id': identity,
                               'name': block['name'], 'arguments': block['text'], 'status': 'completed'})
            elif key == 'thinking':
                output.append({'id': identity, 'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': block['text']}]})
            else:
                output.append({'id': identity, 'type': 'message', 'role': 'assistant', 'status': 'completed',
                               'content': [{'type': 'output_text', 'text': block['text'], 'annotations': []}]})
        result['output'] = output
        return result

    def finish(self, final_response=None):
        result = converted(self.collector.result(), self.protocol, self.thinking_display)
        if self.protocol == 'responses' and final_response is not None:
            for field in ('store', 'previous_response_id'):
                if field in final_response:
                    result[field] = final_response[field]
        events = []
        if self.protocol == "messages":
            if self.message_active is not None:
                events.append(self.event('content_block_stop', index=self.message_index))
                self.message_active = None
            for block in result['content']:
                if block['type'] != 'tool_use':
                    continue
                self.message_index += 1
                start = {**block, 'input': {}}
                events.append(self.event('content_block_start', index=self.message_index, content_block=start))
                events.append(self.event('content_block_delta', index=self.message_index,
                                         delta={'type': 'input_json_delta', 'partial_json': json.dumps(block['input'], ensure_ascii=False)}))
                events.append(self.event('content_block_stop', index=self.message_index))
            events.append(self.event("message_delta", delta={"stop_reason": result["stop_reason"], "stop_sequence": None}, usage=result["usage"]))
            events.append(self.event("message_stop"))
        else:
            output = []
            for key, block in self.blocks.items():
                tool = key.startswith("tool:")
                identity, index = block["id"], block["index"]
                if tool:
                    item = {"id": "fc_" + identity, "type": "function_call", "call_id": identity, "name": block["name"], "arguments": block["text"], "status": "completed"}
                    events.append(self.event("response.function_call_arguments.done", item_id=item["id"], output_index=index, arguments=block["text"]))
                elif key == "thinking":
                    part = {"type": "summary_text", "text": block["text"]}
                    item = {"id": identity, "type": "reasoning", "summary": [part]}
                    events.append(self.event("response.reasoning_summary_text.done", item_id=identity, output_index=index, summary_index=0, text=block["text"]))
                    events.append(self.event("response.reasoning_summary_part.done", item_id=identity, output_index=index, summary_index=0, part=part))
                else:
                    part = {"type": "output_text", "text": block["text"], "annotations": []}
                    item = {"id": identity, "type": "message", "role": "assistant", "status": "completed", "content": [part]}
                    events.append(self.event("response.output_text.done", item_id=identity, output_index=index, content_index=0, text=block["text"]))
                    events.append(self.event("response.content_part.done", item_id=identity, output_index=index, content_index=0, part=part))
                output.append(item)
                events.append(self.event("response.output_item.done", output_index=index, item=item))
            result["output"] = output
            events.append(self.event("response.incomplete" if result["status"] == "incomplete" else "response.completed", response=result))
        return events
