"""Celery tasks for the Email Receptionist (scheduled in settings.CELERY_BEAT_SCHEDULE)."""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def poll_all_mailboxes_task():
    """Reads new emails from every connected mailbox (beat: every 2 minutes)."""
    from email_receptionist.services.imap_poller_service import IMAPPollerService
    totals = IMAPPollerService.poll_all()
    if totals['ingested'] or totals['errors']:
        logger.info(f"EmailReceptionist poll: {totals}")
    return None


@shared_task(ignore_result=True)
def send_scheduled_reply_task(lead_id):
    """Sends one scheduled autonomous reply once its undo window has passed."""
    from email_receptionist.services.auto_reply_service import AutoReplyService
    AutoReplyService.send_if_due(lead_id)


@shared_task(ignore_result=True)
def dispatch_due_replies_task():
    """Safety sweep (beat: every 30s) for scheduled replies whose countdown task was lost."""
    from email_receptionist.services.auto_reply_service import AutoReplyService
    sent = AutoReplyService.dispatch_due()
    if sent:
        logger.info(f"EmailReceptionist: dispatched {sent} scheduled replies")
