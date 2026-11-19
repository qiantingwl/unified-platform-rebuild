"""Executor regressions: bounded creation, cancelled navigation and proxy wiring."""
import asyncio

import anyio
import pytest

from browser_executor.main import Executor, Fail, OpenInput


class Page:
    def __init__(self, gate):
        self.gate = gate
        self.entered = asyncio.Event()

    async def goto(self, *args, **kwargs):
        self.entered.set()
        await self.gate.wait()


class Context:
    def __init__(self, gate):
        self.page = Page(gate)
        self.closed = False

    async def expose_binding(self, *args):
        pass

    async def add_init_script(self, *args):
        pass

    async def route(self, *args):
        pass

    def on(self, *args):
        pass

    async def new_page(self):
        return self.page

    async def close(self):
        await asyncio.sleep(0)
        self.closed = True


class Browser:
    def __init__(self, gate):
        self.gate = gate
        self.contexts = []
        self.options = []
        self.created = asyncio.Event()

    def is_connected(self):
        return True

    async def new_context(self, **options):
        await asyncio.sleep(0)
        context = Context(self.gate)
        self.contexts.append(context)
        self.options.append(options)
        self.created.set()
        return context


def setup(capacity=1):
    gate = asyncio.Event()
    executor = Executor(capacity, 900, 60)
    executor.browser = Browser(gate)
    return executor, gate, OpenInput(url="https://account.example.test/login")


def test_creation_reserves_capacity_before_first_await_and_duplicate_joins():
    async def run():
        executor, gate, body = setup()
        first = asyncio.create_task(executor.open("first", body))
        await executor.browser.created.wait()
        duplicate = asyncio.create_task(executor.open("first", body))
        with pytest.raises(Fail) as error:
            await executor.open("second", body)
        assert error.value.code == "browser_capacity_busy"
        assert not executor.sessions  # partially navigated sessions are not exposed
        gate.set()
        one, two = await asyncio.gather(first, duplicate)
        assert one is two
        assert len(executor.browser.contexts) == 1
        assert not executor.opening
    asyncio.run(run())


def test_cancelled_navigation_closes_context_and_releases_slot():
    async def run():
        executor, gate, body = setup()
        first = asyncio.create_task(executor.open("first", body))
        await executor.browser.created.wait()
        await executor.browser.contexts[0].page.entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert executor.browser.contexts[0].closed
        assert not executor.sessions and not executor.opening
        gate.set()
        await executor.open("replacement", body)
        assert set(executor.sessions) == {"replacement"}
    asyncio.run(run())


def test_cancel_scope_does_not_interrupt_context_cleanup():
    async def run():
        executor, gate, body = setup()
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as group:
                group.start_soon(executor.open, "first", body)
                await executor.browser.created.wait()
                await executor.browser.contexts[0].page.entered.wait()
                scope.cancel()
        assert executor.browser.contexts[0].closed
        assert not executor.sessions and not executor.opening
    asyncio.run(run())


def test_cancelled_duplicate_does_not_cancel_original_open():
    async def run():
        executor, gate, body = setup()
        first = asyncio.create_task(executor.open("first", body))
        await executor.browser.created.wait()
        duplicate = asyncio.create_task(executor.open("first", body))
        await asyncio.sleep(0)
        duplicate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await duplicate
        gate.set()
        assert await first is executor.sessions["first"]
    asyncio.run(run())


def test_proxy_credentials_and_ipv6_reach_browser_context():
    async def run():
        executor, gate, _ = setup()
        gate.set()
        await executor.open("first", OpenInput(url="https://account.example.test/login", proxy="http://synthetic-user:p%40ss@[::1]:8080"))
        assert executor.browser.options[0]["proxy"] == {
            "server": "http://[::1]:8080", "username": "synthetic-user", "password": "p@ss",
        }
    asyncio.run(run())


@pytest.mark.parametrize("proxy", ["http://proxy:bad", "http://proxy:99999", "http://[broken:8080", "file://proxy:1234"])
def test_invalid_proxy_is_structured_failure_without_context(proxy):
    async def run():
        executor, _, _ = setup()
        with pytest.raises(Fail) as error:
            await executor.open("first", OpenInput(url="https://account.example.test/login", proxy=proxy))
        assert (error.value.code, error.value.status) == ("invalid_proxy_url", 422)
        assert not executor.browser.contexts and not executor.opening
    asyncio.run(run())
