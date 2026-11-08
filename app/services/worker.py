import asyncio
from datetime import datetime, timedelta, timezone
import logging
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_maker
from app.models.outbox import EmailOutbox, OutboxStatus
from app.models.subscription import SubscriptionRequest, SubscriptionStatus
from app.models.webhook import WebhookEvent, WebhookStatus
from app.services.email_client import (
    EmailPermanentError,
    EmailRateLimitError,
    EmailRetryableError,
    email_client,
)
from app.services.webhook_service import verify_and_schedule_event_jobs

logger = logging.getLogger("stockping.worker")


class OutboxWorker:
    def __init__(self, session_maker=None, poll_interval: float | None = None):
        self._session_maker = session_maker
        self.poll_interval = poll_interval or settings.WORKER_POLL_INTERVAL
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def session_maker(self):
        return self._session_maker or async_session_maker

    @session_maker.setter
    def session_maker(self, value):
        self._session_maker = value

    async def recover_expired_leases(self, session: AsyncSession) -> int:
        """
        Recovers jobs stuck in 'processing' whose lease has expired
        (e.g., due to an abrupt container restart, SIGKILL, or unhandled crash).
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(EmailOutbox)
            .where(
                EmailOutbox.status == OutboxStatus.PROCESSING.value,
                EmailOutbox.lease_timeout_at < now,
            )
            .values(
                status=OutboxStatus.PENDING.value,
                lease_timeout_at=None,
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount

    async def retry_pending_webhooks(self, session: AsyncSession) -> int:
        """
        Re-verifies any webhook events that could not verify stock earlier
        due to upstream stock API connection drops or temporary 5xx errors.
        """
        stmt = (
            select(WebhookEvent)
            .where(WebhookEvent.status == WebhookStatus.PENDING.value)
            .limit(10)
        )
        events = (await session.execute(stmt)).scalars().all()
        processed = 0
        for event in events:
            try:
                await verify_and_schedule_event_jobs(session, event)
                processed += 1
            except Exception as exc:
                logger.warning(f"Retry stock verification failed for event {event.event_id}: {exc}")
        return processed

    async def claim_and_process_jobs(self, session: AsyncSession) -> int:
        """
        Atomically claims eligible pending outbox jobs and dispatches emails.
        """
        now = datetime.now(timezone.utc)
        lease_expire = now + timedelta(seconds=settings.WORKER_LEASE_SECONDS)

        # 1. Identify candidate jobs
        candidate_stmt = (
            select(EmailOutbox.id)
            .where(
                EmailOutbox.status == OutboxStatus.PENDING.value,
                EmailOutbox.next_retry_at <= now,
            )
            .order_by(EmailOutbox.next_retry_at)
            .limit(10)
        )
        candidate_ids = (await session.execute(candidate_stmt)).scalars().all()
        if not candidate_ids:
            return 0

        processed_count = 0

        # 2. Claim and execute jobs atomically
        for job_id in candidate_ids:
            claim_stmt = (
                update(EmailOutbox)
                .where(
                    EmailOutbox.id == job_id,
                    EmailOutbox.status == OutboxStatus.PENDING.value,
                )
                .values(
                    status=OutboxStatus.PROCESSING.value,
                    lease_timeout_at=lease_expire,
                )
            )
            claim_res = await session.execute(claim_stmt)
            await session.commit()

            if claim_res.rowcount == 0:
                # Job was claimed by another concurrent worker loop
                continue

            # Fetch claimed job
            job = await session.get(EmailOutbox, job_id)
            if not job:
                continue

            # 3. Deliver email via Resend API
            try:
                await email_client.send_restock_email(job.email, job.sku)

                # Successful delivery confirmed
                sent_timestamp = datetime.now(timezone.utc)
                job.status = OutboxStatus.SENT.value
                job.sent_at = sent_timestamp
                job.lease_timeout_at = None
                job.last_error = None

                # Update associated subscription request
                sub = await session.get(SubscriptionRequest, job.subscription_id)
                if sub:
                    sub.status = SubscriptionStatus.NOTIFIED.value
                    sub.notified_at = sent_timestamp
                    sub.attempts += 1
                    sub.last_error = None

                await session.commit()
                processed_count += 1

            except EmailRateLimitError as exc:
                job.attempts += 1
                job.status = OutboxStatus.PENDING.value
                job.lease_timeout_at = None
                job.last_error = f"Rate limit (429): {exc}"
                delay = exc.retry_after if exc.retry_after else min(5 * (2 ** job.attempts), 300)
                job.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                await session.commit()

            except EmailRetryableError as exc:
                job.attempts += 1
                job.lease_timeout_at = None
                job.last_error = f"Retryable error: {exc}"
                if job.attempts >= settings.WORKER_MAX_ATTEMPTS:
                    job.status = OutboxStatus.FAILED.value
                else:
                    job.status = OutboxStatus.PENDING.value
                    delay = min(2 ** job.attempts, 120)
                    job.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                await session.commit()

            except EmailPermanentError as exc:
                job.attempts += 1
                job.status = OutboxStatus.FAILED.value
                job.lease_timeout_at = None
                job.last_error = f"Permanent error: {exc}"

                sub = await session.get(SubscriptionRequest, job.subscription_id)
                if sub:
                    sub.attempts += 1
                    sub.last_error = str(exc)

                await session.commit()

            except Exception as exc:
                job.attempts += 1
                job.lease_timeout_at = None
                job.last_error = f"Unexpected delivery error: {exc}"
                if job.attempts >= settings.WORKER_MAX_ATTEMPTS:
                    job.status = OutboxStatus.FAILED.value
                else:
                    job.status = OutboxStatus.PENDING.value
                    job.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=10)
                await session.commit()

        return processed_count

    async def run_once(self, session: AsyncSession | None = None) -> dict[str, int]:
        """
        Executes a single pass of lease recovery, pending webhook retries,
        and outbox job dispatching. Supports passing an explicit session for testing.
        """
        if session is not None:
            recovered = await self.recover_expired_leases(session)
            webhooks_retried = await self.retry_pending_webhooks(session)
            jobs_processed = await self.claim_and_process_jobs(session)
            return {
                "recovered_leases": recovered,
                "webhooks_retried": webhooks_retried,
                "jobs_processed": jobs_processed,
            }

        async with self.session_maker() as sess:
            recovered = await self.recover_expired_leases(sess)
            webhooks_retried = await self.retry_pending_webhooks(sess)
            jobs_processed = await self.claim_and_process_jobs(sess)
            return {
                "recovered_leases": recovered,
                "webhooks_retried": webhooks_retried,
                "jobs_processed": jobs_processed,
            }

    async def _loop(self):
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in background outbox worker cycle: {exc}", exc_info=True)
            await asyncio.sleep(self.poll_interval)

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("OutboxWorker background task started.")

    async def stop(self):
        if self._running:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("OutboxWorker background task stopped.")


worker = OutboxWorker()
