import asyncio

from app.worker import MaintenanceWorker


def test_fast_jobs_have_bounded_dispatch_rounds_and_fresh_heartbeat(monkeypatch):
    """An endless backlog must not trap the runner inside its refill loop."""
    class Backlog:
        claims = 0

        def clock(self):
            return 1000 + self.claims

        def claim(self, owner, capacity):
            self.claims += 1
            return {"id": self.claims}

    async def run():
        store = Backlog()
        worker = MaintenanceWorker(store, capacity=3)
        ticks, completed = [], asyncio.Event()

        async def immediate(job):
            ticks.append(worker.last_tick)
            if len(ticks) >= worker.capacity:
                completed.set()

        async def threaded(function, *args):
            # Every previously dispatched task finishes while claim is in
            # flight. Thus len(tasks) never reaches the capacity threshold.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return function(*args)

        monkeypatch.setattr(worker, "execute", immediate)
        monkeypatch.setattr(asyncio, "to_thread", threaded)
        runner = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(completed.wait(), 1)
            await asyncio.sleep(.02)
            assert store.claims == 3
            assert ticks == [1001, 1002, 1003]
            assert worker.last_tick == 1003
            await asyncio.sleep(.32)
            assert store.claims == 6
            assert ticks[-3:] == [1004, 1005, 1006]
            assert worker.capacity == 3
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        assert not worker.running and not worker.tasks

    asyncio.run(run())
