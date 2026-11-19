"""Bounded task-centre facts shared by the existing jobs endpoint."""
from sqlalchemy import func, select
from .models import Account, Attempt, Job


def enrich_jobs(session, records, now):
    """Batch queries only for the displayed page, with no historical JSON scan."""
    if not records:
        return {}
    ids = [j.id for j in records]
    accounts = {a.id: a for a in session.scalars(select(Account).where(Account.id.in_({j.account_id for j in records})))}
    latest = select(Attempt.job_id, func.max(Attempt.number).label("number")).where(Attempt.job_id.in_(ids)).group_by(Attempt.job_id).subquery()
    attempts = {a.job_id: a for a in session.scalars(select(Attempt).join(latest,
        (Attempt.job_id == latest.c.job_id) & (Attempt.number == latest.c.number)))}
    result = {}
    for job in records:
        account, attempt = accounts[job.account_id], attempts.get(job.id)
        if job.status == "queued":
            reason = "scheduled_backoff" if job.available_at > now else "awaiting_claim"
            if account.credential_version != job.credential_version:
                reason = "credential_version_changed"
        elif job.status == "running":
            reason = "lease_expired_pending_recovery" if (job.lease_until or 0) <= now else "executing"
        elif job.status == "uncertain":
            reason = "receipt_pending"
        else:
            reason = job.error_code or job.result_code or job.status
        started = attempt.started_at if attempt else None
        from .account_directory import identity
        display = identity(account)
        result[job.id] = {**display, "label": display["display_name"], "platform": account.platform,
            "queue_age_seconds": max(0, (started or job.finished_at or now)-job.created_at),
            "execution_started_at": started,
            "execution_seconds": max(0, (attempt.finished_at or now)-started) if started and attempt else None,
            "wait_reason": reason, "receipt_pending": job.status == "uncertain",
            "next_action_at": job.available_at if job.status == "queued" else None,
            "credential_current": account.credential_version == job.credential_version}
    return result
