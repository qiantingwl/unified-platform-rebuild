import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet
from app.store import Store


@pytest.mark.skipif(not (os.getenv("UNIFIED_TEST_DATABASE_URL") or os.getenv("UNIFIED_TEST_POSTGRES") == "1"), reason="PostgreSQL startup race")
def test_api_and_tasks_initialize_same_database_concurrently(tmp_path):
    url = f"sqlite:///{tmp_path / 'shared-startup'}"
    key = Fernet.generate_key()
    barrier = Barrier(2)
    def start(_):
        barrier.wait(timeout=10)
        return Store(url, key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(start, range(2)))
    account = first.create_account("Loomy", "startup-check", "startup-check")
    with second.read() as (session, state):
        assert second.account(session, account).external_id == "startup-check"
        assert state.schema_version == 2 and state.revision > 0
