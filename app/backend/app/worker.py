import asyncio
import json
from uuid import uuid4
from contextlib import aclosing

from .store import DomainError
from .upstream import UpstreamError, frame_failure
from .maintenance_proxy import resolve_maintenance_proxy


class MaintenanceWorker:
    """One shared maintenance runner. The synthetic branch exists for state-machine tests only."""

    HEARTBEAT_SECONDS = 10
    CLOSE_SECONDS = 5

    def __init__(self, store, capacity=8, registry=None):
        self.store, self.capacity = store, capacity
        self.registry = registry
        self.owner = "worker-" + str(uuid4())
        self.tasks = set()
        self.running = False
        self.last_tick = None
        self.last_error = None

    async def execute(self, job):
        if job.get("sandbox") is not True:
            return await self.execute_real(job)
        try:
            await asyncio.sleep(0.25)
            kind = job["kind"]
            await asyncio.to_thread(
                self.store.finish, job["id"], job["owner"], job["attempt"],
                kind in {"checkin", "balance", "verify"},
                error=None if kind in {"checkin", "balance", "verify"} else "adapter_unavailable",
                balance=2100 if kind == "balance" else None,
                effective_answer=kind == "verify",
            )
        except DomainError as error:
            self.last_error = error.code
        except asyncio.CancelledError:
            # Lease recovery records uncertainty; never invent a completion.
            raise
        except Exception:
            self.last_error = "worker_internal_error"

    async def execute_real(self, job):
        adapter = None
        proxy = None
        execution = asyncio.current_task()
        lease_lost = False
        receipt_committed = False
        receipt_fence = asyncio.Lock()
        from .proxy_pool import record_proxy_result

        async def commit_receipt(method, *args, **kwargs):
            nonlocal receipt_committed
            # A heartbeat must not inspect the row between its terminal commit
            # and our acknowledgement, or normal completion looks like a lost
            # lease. The store still checks the owner/attempt/expiry fence.
            async with receipt_fence:
                result = await asyncio.to_thread(method, *args, **kwargs)
                receipt_committed = True
                return result

        async def renew_lease():
            nonlocal lease_lost
            try:
                while True:
                    await asyncio.sleep(self.HEARTBEAT_SECONDS)
                    async with receipt_fence:
                        if receipt_committed:
                            return
                        await asyncio.to_thread(self.store.heartbeat_job, job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Losing the fence also stops the upstream operation. Otherwise
                # another worker can recover this lease while this one continues
                # consuming a shared slot and producing an unowned result.
                lease_lost = True
                self.last_error = "maintenance_lease_lost"
                execution.cancel()
        pulse = asyncio.create_task(renew_lease())
        try:
            if job["kind"] == "invitation":
                result = await self.invitations.execute_job(job)
                await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], not result.get("error"), error=result.get("error"))
                return
            credential = await asyncio.to_thread(self.store.credential, job["account_id"], job["version"])
            proxy = await asyncio.to_thread(resolve_maintenance_proxy, self.store, job["account_id"], job["kind"])
            adapter = self.registry.make(job["platform"], credential, job["account_id"], job["version"], proxy)
            async def ledger(key, value=None):
                # Shared store validates owner/attempt/version for every ledger
                # write, not just the final job receipt.
                return await asyncio.to_thread(self.store.operation_ledger, job["account_id"], key, value, fence=job)
            adapter.ledger = ledger
            async with asyncio.timeout(120):
                if job["kind"] == "balance":
                    balance = await adapter.balance()
                    if job["platform"] in {"WorkBuddy 国内", "WorkBuddy 国际"}:
                        from .workbuddy_credits import observe
                        try:
                            async with asyncio.timeout(15):
                                packages = await observe(adapter, job["platform"], job["version"])
                            await ledger("credit_packages", packages)
                        except (UpstreamError, TimeoutError) as error:
                            previous = await ledger("credit_packages") or {}
                            # A failed package query cannot erase a valid scalar
                            # balance or relabel an old snapshot as current.
                            await ledger("credit_packages", {**previous, "status": "failed",
                                "error": getattr(error, "code", "upstream_timeout"), "last_attempt_at": self.store.clock()})
                    await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], True, balance=balance)
                elif job["kind"] == "checkin":
                    from .store import day_at
                    current_day_only = job["platform"] in {"WorkBuddy 国内", "WorkBuddy 国际"}
                    if current_day_only and day_at(self.store.clock()) != job["day"]:
                        raise UpstreamError("checkin_readback_unavailable")
                    receipt = await adapter.checkin_receipt(job["day"]) if job.get("reconcile_checkin") else await adapter.checkin(job["day"])
                    if receipt.get("day") != job["day"] or current_day_only and day_at(self.store.clock()) != job["day"]:
                        raise UpstreamError("unconfirmed_checkin")
                    await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], True)
                elif job["kind"] == "verify":
                    answered, ended, finish_deadline = False, False, None
                    messages = [{"role": "user", "content": "Reply with OK."}]
                    if job["platform"] in {"WorkBuddy 国内", "WorkBuddy 国际"}:
                        # This is our own recovery probe, not client history.
                        # WorkBuddy's official probe contract requires system first.
                        messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})
                    async with aclosing(adapter.chat({"model": job["model"], "messages": messages, "max_tokens": 16, "stream": True}, "verify-" + job["id"])) as stream:
                        while True:
                            # Some adapters report finish_reason before the tail.
                            # Bound the tail wait, but never ignore an explicit error.
                            timeout = asyncio.timeout_at(finish_deadline)
                            try:
                                async with timeout:
                                    frame = await anext(stream)
                            except StopAsyncIteration:
                                break
                            except TimeoutError:
                                if not timeout.expired():
                                    raise
                                break
                            failure = frame_failure(frame)
                            if failure:
                                raise failure
                            if frame.get("_done"):
                                ended = True
                                break
                            choices = frame.get("choices") or []
                            if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
                                raise UpstreamError("invalid_stream_frame")
                            for choice in choices:
                                delta = choice.get("delta") or {}
                                if not isinstance(delta, dict):
                                    raise UpstreamError("invalid_stream_frame")
                                text = delta.get("content")
                                answered |= isinstance(text, str) and bool(text.strip())
                                finish = choice.get("finish_reason")
                                if finish == "content_filter":
                                    raise UpstreamError("content_filter", 400)
                                if isinstance(finish, str) and finish.strip() and not ended:
                                    ended = True
                                    finish_deadline = asyncio.get_running_loop().time() + 1
                    if not ended:
                        raise UpstreamError("truncated_stream", 502, True)
                    if not answered:
                        raise UpstreamError("no_effective_output", 502, True)
                    await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], True, effective_answer=True)
                elif job["kind"] == "renew":
                    replacement = await asyncio.to_thread(self.store.renewal_candidate, job)
                    if replacement is None:
                        key = "renewal_intent:" + str(job["version"])
                        intent = await ledger(key) or {}
                        if intent and not intent.get("safe_to_retry"):
                            raise UpstreamError("previous_operation_requires_review", 409)
                        intent = {"status": "started", "job_id": job["id"], "attempt": job["attempt"],
                                  "credential_version": job["version"], "business_day": job["day"],
                                  "started_at": self.store.clock(), "safe_to_retry": False}
                        await ledger(key, intent)
                        try:
                            replacement = await adapter.renew()
                        except UpstreamError as error:
                            # Only a proven pre-send failure permits automatic
                            # replay of a potentially single-use refresh token.
                            safe = error.failure_phase in {"connect", "proxy_connect", "local_pool"}
                            await ledger(key, {**intent, "status": "not_sent" if safe else "uncertain",
                                               "error": error.code, "safe_to_retry": safe})
                            raise
                        await asyncio.to_thread(self.store.renewal_candidate, job, replacement)
                        await ledger(key, {**intent, "status": "issued"})
                    verified = await adapter.validate_renewal(replacement)
                    await commit_receipt(self.store.finish_login, job["id"], job["owner"], job["attempt"], json.dumps(replacement), verified)
                elif job["kind"] == "onboarding":
                    result = await adapter.onboarding_receipt() if job.get("reconcile_onboarding") else await adapter.onboarding()
                    success = result.get("settled") is True and not result.get("failed")
                    uncertain = any(t.get("status") in {"claiming", "uncertain"} for t in result.get("tasks", []))
                    await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], success,
                                            error=None if success else "onboarding_uncertain" if uncertain else "onboarding_incomplete")
                elif job["kind"] == "benefits":
                    from .workbuddy_benefits import execute, inspect
                    if job.get("benefits_read_only"):
                        result = await inspect(adapter,job["platform"],job=job)
                        success = result["status"] == "verified"
                    else:
                        result = await execute(adapter, job["platform"], day=job.get("day"), reconcile=job.get("reconcile_benefits", False), job=job)
                        success = result["settled"]
                    await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], success,
                        error=result.get("error_code"))
                else:
                    # Renewal may rotate a token. It is not activated until an
                    # identity verifier exists for the platform's response shape.
                    raise UpstreamError("adapter_unavailable", 501)
            try:
                await asyncio.to_thread(record_proxy_result, self.store, job["account_id"], proxy, success=True)
            except Exception:
                self.last_error = "proxy_observation_failed"
        except (UpstreamError, DomainError, TimeoutError) as failure:
            code = getattr(failure, "code", "upstream_timeout")
            try:
                await asyncio.to_thread(record_proxy_result, self.store, job["account_id"], proxy, success=False, code=code,
                                        failure_phase=getattr(failure, "failure_phase", None))
            except Exception:
                self.last_error = "proxy_observation_failed"
            try:
                await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], False, error=code)
            except DomainError as stale:
                self.last_error = stale.code
        except asyncio.CancelledError:
            if lease_lost:
                return  # Lease recovery decides queued versus uncertain.
            raise
        except Exception:
            self.last_error = "worker_internal_error"
            await commit_receipt(self.store.finish, job["id"], job["owner"], job["attempt"], False, error="internal_error")
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            if adapter:
                try:
                    async with asyncio.timeout(self.CLOSE_SECONDS):
                        await adapter.t.close()
                except Exception:
                    self.last_error = "adapter_close_failed"

    def completed(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.last_error = "worker_internal_error"

    async def run(self):
        self.running = True
        try:
            while True:
                self.last_tick = self.store.clock()
                # Fast jobs can finish while the next claim runs, so the
                # active-task count alone never bounds an endless backlog.
                # Cap this refill pass, not the configured execution capacity.
                dispatched, pass_limit = 0, self.capacity
                while len(self.tasks) < self.capacity and dispatched < pass_limit:
                    try:
                        job = await asyncio.to_thread(self.store.claim, self.owner, self.capacity)
                    except Exception:
                        self.last_error = "database_unavailable"
                        break
                    if job is None:
                        break
                    self.last_tick = self.store.clock()
                    task = asyncio.create_task(self.execute(job))
                    self.tasks.add(task)
                    task.add_done_callback(self.completed)
                    dispatched += 1
                await asyncio.sleep(0.3)
        finally:
            self.running = False
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

