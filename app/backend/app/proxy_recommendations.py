"""Refresh the site's measured recommendation list; never treat site speed as OVH health."""
import asyncio
import json
import math
import re
from html.parser import HTMLParser

from .proxy_protocols import parse_node
from .store import DomainError

SOURCE_URL = 'https://jiedianleida.com/'
MAX_BYTES = 4 * 1024 * 1024


class _DataScript(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == 'nodes-data':
            self.active = True
            self.count += 1

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_recommended(html, *, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise DomainError('invalid_recommendation_limit', 422)
    if len(html.encode('utf8')) > MAX_BYTES:
        raise DomainError('subscription_too_large', 413)
    script = _DataScript()
    try:
        script.feed(html)
        if script.count != 1:
            raise ValueError()
        data = json.loads(''.join(script.parts))
        rows = data['best']
        if not isinstance(rows, list) or len(rows) > 10000 or data.get('tested') is not True or data.get('test_mode') != 'real':
            raise ValueError()
    except (ValueError, KeyError, TypeError, RecursionError):
        raise DomainError('invalid_recommendation_source', 422) from None
    vantages = data.get('vantages', [])
    if not isinstance(vantages, list) or len(vantages) > 32:
        raise DomainError('invalid_recommendation_source', 422)
    candidates, rejected = [], []
    selection_complete = True
    for rank, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 5:
            selection_complete = False
            rejected.append({'index':rank, 'reason':'invalid_recommendation_metrics'})
            continue
        uri, stable, latency, speeds, seen = row
        if not isinstance(uri, str) or not _number(stable) or not _number(seen) or not isinstance(latency, list) or not isinstance(speeds, list) or len(speeds) != len(vantages) or len(latency) != len(vantages):
            selection_complete = False
            rejected.append({'index':rank, 'reason':'invalid_recommendation_metrics'})
            continue
        if not isinstance(uri, str) or not _number(stable) or stable <= 0 or not isinstance(speeds, list):
            rejected.append({'index':rank, 'reason':'unmeasured_recommendation'})
            continue
        measured = [(i, speed) for i, speed in enumerate(speeds) if _number(speed) and speed > 0]
        if not measured:
            rejected.append({'index':rank, 'reason':'unmeasured_recommendation'})
            continue
        best_speed = max(speed for _, speed in measured)
        locations = []
        for i, _ in measured:
            if i < len(vantages) and isinstance(vantages[i], dict):
                code = vantages[i].get('cc', '')
                if isinstance(code, str) and re.fullmatch('[A-Z]{2}', code):
                    locations.append(code)
        candidates.append((best_speed, rank, uri, locations))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    nodes, seen_ids = [], set()
    supported = 0
    for speed, rank, uri, locations in candidates:
        try:
            node = parse_node(uri)
        except DomainError as error:
            rejected.append({'index':rank, 'reason':error.code})
            continue
        if node['identity'] in seen_ids:
            continue
        seen_ids.add(node['identity'])
        supported += 1
        if len(nodes) < limit:
            # The source labels this value MB/s; preserve the value and state its unit.
            node['source_metrics'] = {'speed_mbps':speed, 'speed_unit':'MB/s', 'rank':rank + 1, 'vantages':locations}
            nodes.append(node)
    if not nodes:
        raise DomainError('subscription_empty', 422)
    stamp = data.get('updated_at')
    if not isinstance(stamp, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.+Z-]{5,32}', stamp):
        stamp = None
    metadata = {'source_updated_at':stamp, 'considered':len(rows), 'speed_tested':len(candidates),
                'supported':supported, 'selected':len(nodes), 'limit':limit, 'speed_unit':'MB/s',
                'speed_origin':'source_reported', 'ovh_verified':False, 'selection_complete':selection_complete}
    return nodes, rejected, metadata


async def fetch_recommended(transport, *, limit=100):
    try:
        async with asyncio.timeout(30):
            async with transport.client.stream('GET', SOURCE_URL, follow_redirects=False,
                    headers={'User-Agent':'Mozilla/5.0', 'Accept':'text/html'}) as response:
                if response.status_code != 200:
                    raise DomainError('subscription_fetch_failed', 502)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise DomainError('subscription_too_large', 413)
                    chunks.append(chunk)
        html = b''.join(chunks).decode('utf-8-sig')
        return await asyncio.to_thread(parse_recommended, html, limit=limit)
    except DomainError:
        raise
    except (ValueError, UnicodeError):
        raise DomainError('invalid_recommendation_source', 422) from None
    except Exception:
        raise DomainError('subscription_fetch_failed', 502) from None
