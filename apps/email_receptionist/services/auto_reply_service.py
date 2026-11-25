"""
AutoReplyService — autonomous mode, undo window and coverage schedule.

Flow for an inbound email when autonomous mode is ON:
  1. AI decides "reply"            -> lead.status = 'scheduled',
                                      lead.scheduled_send_at = now + auto_reply_delay_seconds
  2. Undo window                    -> a user can cancel from the dashboard (status back to 'awaiting')
  3. Celery sends when due          -> send_scheduled_reply_task (countdown) + dispatch_due_replies_task (safety sweep)

The reply is NOT auto-sent (stays 'awaiting' for a human) when:
  * autonomous mode is off
  * the AI chose "review" or "escalate"
  * we are outside the coverage schedule
  * the thread already reached max_auto_replies_per_thread
  * the lead is spam / has no email address
"""
import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

logger = logging.getLogger(__name__)


class AutoReplyService:

    # ── Coverage schedule ────────────────────────────────────────────────
    @staticmethod
    def _local_now(cfg, now=None):
        now = now or timezone.now()
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo(cfg.business_timezone or 'UTC'))
        except Exception:
            return now

    @classmethod
    def is_business_hours(cls, cfg, now=None) -> bool:
        local = cls._local_now(cfg, now)
        
        if local.weekday() >= 5:  # Sat / Sun
            return False
        start, end = cfg.business_hours_start, cfg.business_hours_end
        t = local.time()
        if start <= end:
            return start <= t < end
        return t >= start or t < end  # overnight window

    @classmethod
    def is_within_coverage(cls, cfg, now=None) -> bool:
        schedule = getattr(cfg, 'coverage_schedule', '24_7') or '24_7'
        if schedule == 'business_hours':
            return cls.is_business_hours(cfg, now)
        if schedule == 'after_hours':
            return not cls.is_business_hours(cfg, now)
        return True

    # ── Scheduling ───────────────────────────────────────────────────────
    @classmethod
    def maybe_schedule(cls, lead, cfg, decision: dict) -> str:
        """
        Decides whether to schedule an autonomous reply. Returns a short outcome string:
        'scheduled' | 'disabled' | 'needs_review' | 'escalated' | 'outside_coverage' | 'limit_reached' | 'skipped'
        """
        action = (decision or {}).get('action', 'review')

        from email_receptionist.services.ai_triage_service import AITriageService
        if lead.is_spam or not lead.sender_email or action == 'ignore' or AITriageService.is_automated_sender(lead.sender_email):
            return 'skipped'

        if action == 'escalate':
            lead.status = 'escalated'
            lead.escalation_reason = decision.get('reason', '') or 'AI flagged this email for a human.'
            lead.scheduled_send_at = None
            lead.save(update_fields=['status', 'escalation_reason', 'scheduled_send_at', 'updated_at'])
            if cfg and cfg.is_autonomous_mode:
                cls.notify_escalation(lead, reason=lead.escalation_reason)
                if lead.ai_draft_response and cls.is_within_coverage(cfg) and lead.auto_reply_count < (cfg.max_auto_replies_per_thread or 0):
                    from email_receptionist.services.email_dispatch_service import EmailDispatchService
                    if EmailDispatchService.dispatch_receptionist_reply(lead=lead, draft_text=lead.ai_draft_response):
                        lead.status = 'escalated'
                        lead.auto_reply_count = (lead.auto_reply_count or 0) + 1
                        lead.save(update_fields=['status', 'auto_reply_count', 'updated_at'])
            return 'escalated'

        if not cfg or not cfg.is_autonomous_mode:
            return 'disabled'

        if action != 'reply':
            lead.escalation_reason = decision.get('reason', '') or 'AI asked for a human review.'
            lead.save(update_fields=['escalation_reason', 'updated_at'])
            return 'needs_review'

        if not cls.is_within_coverage(cfg):
            lead.escalation_reason = 'Outside the autonomous coverage schedule - waiting for a human.'
            lead.save(update_fields=['escalation_reason', 'updated_at'])
            return 'outside_coverage'

        if lead.auto_reply_count >= (cfg.max_auto_replies_per_thread or 0):
            lead.escalation_reason = (f'Auto-reply limit reached ({cfg.max_auto_replies_per_thread} in this thread) '
                                      f'- a human should take over.')
            lead.save(update_fields=['escalation_reason', 'updated_at'])
            return 'limit_reached'

        delay = max(0, int(cfg.auto_reply_delay_seconds or 0))
        lead.status = 'scheduled'
        lead.scheduled_send_at = timezone.now() + timedelta(seconds=delay)
        lead.escalation_reason = ''
        lead.save(update_fields=['status', 'scheduled_send_at', 'escalation_reason', 'updated_at'])

        try:
            from email_receptionist.tasks import send_scheduled_reply_task
            send_scheduled_reply_task.apply_async((lead.id,), countdown=delay)
        except Exception as e:
            # Broker down: the dispatch_due_replies_task beat sweep will still send it.
            logger.warning(f'EmailReceptionist: could not enqueue scheduled reply for lead #{lead.id}: {e}')
        return 'scheduled'

    @staticmethod
    def cancel(lead) -> bool:
        from email_receptionist.models import ReceptionistLead
        updated = ReceptionistLead.objects.filter(id=lead.id, status='scheduled').update(
            status='awaiting', scheduled_send_at=None,
            escalation_reason='Auto-send cancelled by a user.', updated_at=timezone.now(),
        )
        return bool(updated)

    @classmethod
    def send_if_due(cls, lead_id: int) -> bool:
        """Sends a scheduled reply if it is still scheduled and due. Safe to call more than once."""
        from email_receptionist.models import ReceptionistLead, ReceptionistConfig
        from email_receptionist.services.email_dispatch_service import EmailDispatchService

        now = timezone.now()
        # Atomic claim: only one worker can flip scheduled_send_at from a due time to NULL.
        claimed = ReceptionistLead.objects.filter(
            id=lead_id, status='scheduled', scheduled_send_at__isnull=False, scheduled_send_at__lte=now,
        ).update(scheduled_send_at=None)
        if not claimed:
            return False

        lead = ReceptionistLead.objects.select_related('tenant').get(id=lead_id)
        cfg = ReceptionistConfig.objects.filter(tenant=lead.tenant).first()
        if not cfg or not cfg.is_autonomous_mode:
            lead.status = 'awaiting'
            lead.escalation_reason = 'Autonomous mode was turned off before sending.'
            lead.save(update_fields=['status', 'escalation_reason', 'updated_at'])
            return False

        ok = EmailDispatchService.dispatch_receptionist_reply(lead=lead, draft_text=lead.ai_draft_response)
        if ok:
            lead.refresh_from_db()
            lead.auto_reply_count = (lead.auto_reply_count or 0) + 1
            lead.save(update_fields=['auto_reply_count', 'updated_at'])
        return ok

    @classmethod
    def dispatch_due(cls) -> int:
        from email_receptionist.models import ReceptionistLead
        due_ids = list(ReceptionistLead.objects.filter(
            status='scheduled', scheduled_send_at__isnull=False, scheduled_send_at__lte=timezone.now(),
        ).values_list('id', flat=True)[:200])
        sent = 0
        for lead_id in due_ids:
            try:
                if cls.send_if_due(lead_id):
                    sent += 1
            except Exception as e:
                logger.error(f'EmailReceptionist: scheduled send failed for lead #{lead_id}: {e}', exc_info=True)
        return sent

    # ── Turning autonomous mode on ───────────────────────────────────────
    PENDING_WINDOW_HOURS = 24

    @classmethod
    def schedule_pending(cls, cfg) -> int:
        """
        Called when autonomous mode is switched ON: emails that arrived in the last 24 hours,
        are still waiting, and that the AI judged safe to answer ("reply") get scheduled now,
        instead of only emails that arrive after the switch. Returns how many were scheduled.
        """
        from email_receptionist.models import ReceptionistLead
        if not cfg or not cfg.is_autonomous_mode:
            return 0
        since = timezone.now() - timedelta(hours=cls.PENDING_WINDOW_HOURS)
        qs = (ReceptionistLead.objects
              .filter(tenant=cfg.tenant, status='awaiting', ai_action='reply', is_spam=False,
                      lead_type='inbound_receptionist', updated_at__gte=since)
              .exclude(sender_email__isnull=True).exclude(sender_email='').exclude(ai_draft_response=''))
        count = 0
        for lead in qs[:200]:
            if cls.maybe_schedule(lead, cfg, {'action': 'reply'}) == 'scheduled':
                count += 1
        return count

    # ── Escalation notification ──────────────────────────────────────────
    @staticmethod
    def escalation_recipients(cfg, lead=None) -> list:
        """Addresses from 'Send escalation alerts to'; if none, the organization owner."""
        raw = (getattr(cfg, 'escalation_emails', '') or '') if cfg else ''
        out = []
        for part in raw.replace(';', ',').replace('\n', ',').split(','):
            addr = part.strip()
            if not addr:
                continue
            try:
                validate_email(addr)
            except ValidationError:
                continue
            if addr.lower() not in [a.lower() for a in out]:
                out.append(addr)
        if out:
            return out
        tenant = (cfg.tenant if cfg else None) or (lead.tenant if lead else None)
        owner_email = getattr(getattr(tenant, 'owner', None), 'email', None) if tenant else None
        return [owner_email] if owner_email else []

    @classmethod
    def notify_escalation(cls, lead, reason: str = '', user=None) -> str:
        """Emails the team that a conversation needs a person. Returns 'mailbox' | 'platform' | 'failed' | 'no_recipient'."""
        from email_receptionist.models import ReceptionistConfig
        from email_receptionist.services.email_dispatch_service import EmailDispatchService
        cfg = ReceptionistConfig.objects.filter(tenant=lead.tenant).select_related('tenant').first() if lead.tenant else None
        recipients = cls.escalation_recipients(cfg, lead)
        if not recipients:
            return 'no_recipient'
        subject = f"[Action Required] Email escalated: {lead.sender_name} ({lead.company_name or lead.sender_email})"
        body = (
            "An email conversation needs a human reply.\n\n"
            f"From: {lead.sender_name} <{lead.sender_email}>\n"
            f"Company: {lead.company_name or 'N/A'}\n"
            f"Subject: {lead.subject}\n"
            f"Reason: {reason or lead.escalation_reason or 'Escalated by a user'}\n\n"
            f"Latest message:\n{(lead.inbound_message or '')[:2000]}\n\n"
            "Open the Email Receptionist inbox to review and reply."
        )
        try:
            return EmailDispatchService.send_internal_alert(cfg, recipients, subject, body)
        except Exception as e:
            logger.warning(f'EmailReceptionist: could not send escalation notification: {e}')
            return 'failed'
