"""Normalize observed provider catalog contracts, not arbitrary nested names."""
from .upstream import UpstreamError


def array(value):
    return value if isinstance(value,list) else []


def reasoning_facts(row):
    """Only declared channel facts; never borrow a similarly named model's limits."""
    native = row.get('reasoning') if isinstance(row.get('reasoning'), dict) else {}
    facts = {}
    def effort(value):
        return isinstance(value, str) and 0 < len(value) <= 40 and not any(ord(c) < 32 for c in value)
    levels = native.get('supportedEfforts')
    if levels is None:
        levels = next((row[k] for k in ('reasoningLevels', 'reasoningEfforts', 'efforts') if k in row), None)
    if isinstance(levels, list) and len(levels) <= 20 and all(effort(v) for v in levels):
        facts['supported_efforts'] = list(dict.fromkeys(levels))
    binding = row.get('reasoningBindingSupported')
    if type(binding) is bool:
        facts['binding_supported'] = binding
        if not binding:
            facts['supported_efforts'] = []
    fixed = row.get('reasoningFixedLevel')
    if binding is not False and effort(fixed):
        facts['fixed_effort'] = fixed
        facts['supported_efforts'] = [fixed]
    default = row.get('reasoningDefaultLevel', native.get('defaultEffort', native.get('effort')))
    if effort(default) and ('supported_efforts' not in facts or default in facts['supported_efforts']):
        facts['default_effort'] = default
    if type(native.get('canDisableThinking')) is bool:
        facts['can_disable_thinking'] = native['canDisableThinking']
    return facts


def catalog_details(value):
    """Keep public catalog facts only; absent capabilities remain unknown.

    Adapters may return normalized rows or a simple models/data/list envelope.
    Never persist a raw upstream row: it may contain account/session material.
    These observations do not grant or deny routing and do not override policy.
    """
    if isinstance(value, dict):
        value = value.get("models", value.get("data", value.get("list", [])))
    if not isinstance(value, list):
        raise UpstreamError("invalid_model_catalog")
    result = {}
    flags = {
        "tools": ("supportsToolCall", "supports_tools", "support_tool_call", "tool_call"),
        "vision": ("supports_images", "supportsImages", "supportsVision", "supports_vision"),
        "multimodal": ("support_multimodal", "supports_multimodal"),
        "reasoning": ("supportsReasoning", "supports_reasoning", "support_reasoning", "supportThinking", "supportsThinking", "support_thinking", "reasoning"),
    }
    limits = {
        "context_window": ("context_window", "contextWindow", "context_length", "max_context_tokens", "maxInputTokens"),
        "max_output_tokens": ("max_output_tokens", "maxOutputTokens", "max_completion_tokens", "max_tokens"),
    }
    for source in value:
        row = {"id": source} if isinstance(source, str) else source
        if not isinstance(row, dict):
            continue
        identity = row.get("id") or row.get("model_id") or row.get("model")
        if not isinstance(identity, str) or not 0 < len(identity) <= 100 or any(c.isspace() for c in identity):
            continue
        nested = [row] + [row[k] for k in ("params", "capabilities", "limits") if isinstance(row.get(k), dict)]
        detail = {"id": identity, "capabilities": {name: None for name in flags}, "context_window": None, "max_output_tokens": None}
        detail['capability_sources'] = {name: None for name in (*flags, *limits)}
        display = row.get("display_name") or row.get("name") or row.get("model_name")
        if isinstance(display, str) and 0 < len(display) <= 200 and not any(ord(c) < 32 for c in display):
            detail["display_name"] = display
        for name, keys in flags.items():
            observations = [obj[key] for obj in nested for key in (name, *keys) if key in obj and type(obj[key]) is bool]
            # Conflicting rows/aliases cannot prove either supported or unsupported.
            if observations and all(v == observations[0] for v in observations):
                detail["capabilities"][name] = observations[0]
                detail['capability_sources'][name] = 'upstream'
        for name, keys in limits.items():
            observations = [obj[key] for obj in nested for key in keys if key in obj and type(obj[key]) is int and 0 < obj[key] <= 1_000_000_000]
            if observations and all(v == observations[0] for v in observations):
                detail[name] = observations[0]
                detail['capability_sources'][name] = 'upstream'
        facts = reasoning_facts(row)
        if facts:
            detail['reasoning'] = facts
            detail['reasoning_source'] = 'upstream'
        for target, source in [('vendor', 'vendor'), ('credits_label', 'credits')]:
            if isinstance(row.get(source), str) and 0 < len(row[source]) <= 100:
                detail[target] = row[source]
        if type(row.get('onlyReasoning')) is bool:
            detail['only_reasoning'] = row['onlyReasoning']
        if identity in result:
            prior = result[identity]
            for name in flags:
                if prior["capabilities"][name] != detail["capabilities"][name]:
                    prior["capabilities"][name] = None
                    prior['capability_sources'][name] = None
            for name in limits:
                if prior[name] != detail[name]:
                    prior[name] = None
                    prior['capability_sources'][name] = None
            if prior.get('reasoning') != detail.get('reasoning'):
                prior.pop('reasoning', None)
                prior.pop('reasoning_source', None)
        else:
            result[identity] = detail
    if not result:
        raise UpstreamError("invalid_model_catalog")
    return [result[key] for key in sorted(result)]


def normalize(platform,value):
    if not isinstance(value,dict):raise UpstreamError("invalid_model_catalog")
    if platform=="小浣熊":
        categories=value.get("categories")
        if not isinstance(categories,list):raise UpstreamError("invalid_model_catalog")
        rows=[{**item,"id":item.get("name") or item.get("model_name")} for cat in categories if isinstance(cat,dict) for item in array(cat.get("models")) if isinstance(item,dict)]
    elif platform=="WPS 灵犀":
        rows=[{**item,"id":item.get("key")} for key in ("plans","models") for item in array(value.get(key)) if isinstance(item,dict)]
    elif platform=="CodeArts":
        raw=value.get("builtinModels")
        if not isinstance(raw,list):raw=(value.get("result") or {}).get("models",[]) if isinstance(value.get("result"),dict) else []
        rows=[{**item,"id":item.get("model_id") or item.get("model_name")} for item in array(raw) if isinstance(item,dict) and item.get("enable") is not False]
    elif platform.startswith("WorkBuddy"):
        raw=value.get("models")
        if not isinstance(raw,list):raise UpstreamError("invalid_model_catalog")
        rows=[item for item in raw if isinstance(item,dict) and item.get("disabled") is not True and not any(isinstance(tag,str) and tag in {"text-to-image","image-to-image","text-to-video","image-to-video"} for tag in array(item.get("tags"))) and not str(item.get("id","")).startswith(("hy-image-","hunyuan-image","kling-video"))]
    else:raise UpstreamError("invalid_model_catalog")
    rows=[item for item in rows if isinstance(item.get("id"),str) and 0<len(item["id"])<=100 and not any(c.isspace() for c in item["id"])]
    if not rows:raise UpstreamError("invalid_model_catalog")
    return rows
