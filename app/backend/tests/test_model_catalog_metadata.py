import json
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.management import mount_management
from app.model_catalog import catalog_details, normalize
from app.store import Store


def test_public_metadata_is_preserved_without_raw_private_fields():
    rows = catalog_details(normalize("小浣熊", {"categories": [{"models": [{
        "name": "wire-id", "model_name": "display", "params": {"context_window": 128000, "max_tokens": 8192, "access_token": "private-param"},
        "supportsToolCall": True, "supports_images": False, "supports_reasoning": True,
        "authorization": "private-header", "credential": {"token": "private-secret"},
    }]}]}))
    assert rows[0]["context_window"] == 128000
    assert rows[0]["max_output_tokens"] == 8192
    assert rows[0]["capabilities"] == {"tools": True, "vision": False, "multimodal": None, "reasoning": True}
    assert "private-" not in json.dumps(rows)


def test_unknown_conflicting_and_invalid_capabilities_remain_unknown():
    rows = catalog_details(["model-a", {"id": "model-b", "supportsToolCall": "true", "max_tokens": True},
                            {"id": "model-c", "supports_images": True, "capabilities": {"vision": False}}])
    for row in rows:
        assert all(v is None for v in row["capabilities"].values())
        assert row["context_window"] is None and row["max_output_tokens"] is None


def test_multimodal_does_not_invent_image_support():
    row = catalog_details(normalize("WPS 灵犀", {"models": [{"key": "m", "support_multimodal": True}]}))[0]
    assert row["capabilities"]["multimodal"] is True
    assert row["capabilities"]["vision"] is None


def test_duplicate_rows_with_conflicting_facts_remain_unknown():
    row = catalog_details([{"id": "m", "supportsToolCall": True}, {"id": "m", "supportsToolCall": False}, {"id": "m", "supportsToolCall": True}])[0]
    assert row["capabilities"]["tools"] is None


def test_channel_reasoning_aliases_levels_and_sources_are_declared_facts():
    rows = catalog_details([
        {'id': 'raccoon', 'params': {'supportThinking': False}, 'reasoningBindingSupported': False, 'reasoningLevels': ['high'], 'reasoningDefaultLevel': 'high'},
        {'id': 'declared', 'support_thinking': True, 'maxInputTokens': 128000, 'reasoningEfforts': ['low', 'high'], 'reasoningDefaultLevel': 'high'},
        {'id': 'fixed', 'reasoningFixedLevel': 'medium', 'reasoningLevels': ['low', 'high']},
        {'id': 'unknown'},
    ])
    by_id = {r['id']: r for r in rows}
    assert by_id['raccoon']['capabilities']['reasoning'] is False
    assert by_id['raccoon']['reasoning'] == {'binding_supported': False, 'supported_efforts': []}
    assert by_id['declared']['reasoning'] == {'supported_efforts': ['low', 'high'], 'default_effort': 'high'}
    assert by_id['declared']['capability_sources']['context_window'] == 'upstream'
    assert by_id['declared']['capability_sources']['reasoning'] == 'upstream'
    assert by_id['fixed']['reasoning']['supported_efforts'] == ['medium']
    assert by_id['unknown']['capability_sources']['reasoning'] is None and 'reasoning' not in by_id['unknown']


def test_conflicting_duplicate_reasoning_and_invalid_levels_do_not_become_facts():
    row = catalog_details([{'id': 'm', 'supportThinking': True, 'reasoningLevels': ['low']},
                           {'id': 'm', 'supportThinking': False, 'reasoningLevels': ['high']},
                           {'id': 'm', 'supportThinking': True, 'reasoningLevels': ['low']}])[0]
    assert row['capabilities']['reasoning'] is None and row['capability_sources']['reasoning'] is None
    assert 'reasoning' not in row and 'reasoning_source' not in row
    invalid = catalog_details([{'id': 'bad', 'reasoningLevels': ['private\nmaterial'], 'reasoningDefaultLevel': 'high'}])[0]
    assert 'supported_efforts' not in invalid.get('reasoning', {})


def test_sync_preserves_admin_policy_and_keeps_legacy_names_contract(tmp_path):
    store = Store(f"sqlite:///{tmp_path/'catalog.db'}", Fernet.generate_key())
    aid = store.create_account("WPS 灵犀", "synthetic", "synthetic")
    store.put_credential(aid, json.dumps({"cookie": "synthetic"}), 0)
    original = {"models": ["old"], "aliases": {"alias": "m"}, "enabled": False, "overrides": {"m": {"enabled": False}}}
    rid = store.resource_put("model_catalog", "WPS 灵犀", original)
    class Adapter:
        async def models(self):
            return [{"id": "m", "context_window": 64000, "supports_images": True, "token": "never-store"}]
        async def close(self):
            pass
    adapter = Adapter()
    adapter.t = adapter
    app = FastAPI()
    app.state.worker = SimpleNamespace()
    mount_management(app, store, SimpleNamespace(make=lambda *args: adapter))
    try:
        with TestClient(app) as client:
            response = client.post(f"/api/accounts/{aid}/models/sync")
            assert response.status_code == 200
            assert response.json()["models"] == ["m"]
            assert response.json()["model_details"][0]["context_window"] == 64000
            public = client.get("/api/catalogs").json()
        saved = store.resources("model_catalog")[0]
        assert saved["id"] == rid
        for key in ("aliases", "enabled", "overrides"):
            assert saved["data"][key] == original[key]
        assert saved["data"]["observed_at"]
        assert "never-store" not in json.dumps(public)
    finally:
        store.engine.dispose()
