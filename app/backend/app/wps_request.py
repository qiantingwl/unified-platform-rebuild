"""WPS wire shape without the reference client's prompt rewriting/trimming."""
from copy import deepcopy

from .upstream import UpstreamError


def build(body):
    messages = []
    for original in body['messages']:
        message = deepcopy(original)
        if message['role'] == 'developer':
            # Native endpoint has no developer role; preserve content and order.
            message['role'] = 'system'
        parts = message.get('content')
        if isinstance(parts, list):
            if any(not isinstance(p, dict) or p.get('type') != 'text' for p in parts):
                raise UpstreamError('unsupported_image_input', 422)
            # A marker at the end still marks the exact same message prefix.
            # A marker mid-message cannot be represented by this native API.
            if any('cache_control' in p for p in parts[:-1]):
                raise UpstreamError('unsupported_protocol_field', 422)
            message['content'] = ''.join(p['text'] for p in parts)
            if parts and 'cache_control' in parts[-1]:
                message['cache_control'] = deepcopy(parts[-1]['cache_control'])
        elif parts is None:
            message['content'] = ''
        messages.append(message)
    outgoing = {'model': body['model'], 'stream': True, 'messages': messages}
    for field in ('tools', 'tool_choice', 'temperature', 'prompt_cache_key', 'prompt_cache_retention', 'parallel_tool_calls'):
        if field in body:
            outgoing[field] = deepcopy(body[field])
    limit = body.get('max_tokens', body.get('max_completion_tokens'))
    if limit is not None:
        outgoing['base_llm_arguments'] = {'max_tokens': limit}
    effort = body.get('reasoning_effort')
    if effort is not None:
        outgoing['reasoning_effort'] = effort
        outgoing['llm_arguments'] = {'reasoning_effort': effort}
    return outgoing
