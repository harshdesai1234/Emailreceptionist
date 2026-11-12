"""
IngestionService — turns one inbound email into a lead thread + AI draft.

Steps:
  1. De-duplicate by Message-ID (an email is never processed twice)
  2. Spam / auto-reply / bulk detection
  3. Find the existing thread (In-Reply-To / References first, then same sender
     with an open thread in the last 30 days) or create a new lead
  4. Store the inbound message
  5. Generate the AI reply with full thread history
  6. Hand the decision to AutoReplyService (autonomous send / escalate / review)
"""
import logging
import re
from datetime import timedelta
from typing import Tuple

from django.utils import timezone

from email_receptionist.models import ReceptionistLead, ReceptionistEmailMessage, ReceptionistConfig
from email_receptionist.services.ai_triage_service import AITriageService
from email_receptionist.services.ai_draft_service import AIDraftService, build_history
from email_receptionist.services.auto_reply_service import AutoReplyService

logger = logging.getLogger(__name__)

THREAD_REUSE_DAYS = 30
MSGID_RE = re.compile(r'<[^<>\s]+>')


def _split_ids(*values) -> list:
    ids = []
    for v in values:
        for m in MSGID_RE.findall(v or ''):
            if m not in ids:
                ids.append(m)
    return ids


class IngestionService:

    @classmethod
    def find_thread(cls, tenant, sender_email: str, in_reply_to: str = '', references: str = ''):
        ref_ids = _split_ids(in_reply_to, references)
        if ref_ids:
            msg = (ReceptionistEmailMessage.objects
                   .filter(lead__tenant=tenant, message_id__in=ref_ids)
                   .select_related('lead').order_by('-sent_at').first())
            if msg:
                return msg.lead
        if sender_email:
            return (ReceptionistLead.objects
                    .filter(tenant=tenant, sender_email__iexact=sender_email,
                            updated_at__gte=timezone.now() - timedelta(days=THREAD_REUSE_DAYS))
                    .exclude(status='archived').exclude(is_spam=True)
                    .order_by('-updated_at').first())
        return None

    @classmethod
    def ingest_lead_or_message(
        cls,
        tenant,
        sender_name: str,
        sender_email: str,
        subject: str,
        message: str,
        phone_number: str = "",
        company_name: str = "",
        role: str = "",
        team_size: str = "",
        est_budget: str = "",
        source: str = "Email",
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        provider_message_id: str = "",
        is_bulk: bool = False,
        recipient: str = "",
    ) -> Tuple[ReceptionistLead, bool, bool]:
        """Returns (lead, created, is_spam)."""
        sender_email = (sender_email or '').strip()
        message = message or ''
        subject = subject or f"Inquiry from {sender_name or sender_email or 'website'}"

        # 1. Never process the same email twice
        if message_id:
            dup = (ReceptionistEmailMessage.objects
                   .filter(lead__tenant=tenant, message_id=message_id, direction='inbound')
                   .select_related('lead').first())
            if dup:
                return dup.lead, False, dup.lead.is_spam

        # 1b. A reply to an email a play sent is handled by the play (routing + timeline), not the receptionist
        if in_reply_to or references or sender_email:
            try:
                from motion.hooks import consume_play_reply
                if consume_play_reply(tenant, sender_email, subject, message, in_reply_to, references, message_id):
                    return None, False, False
            except (ImportError, ModuleNotFoundError):
                pass
            except Exception:
                logger.exception('motion consume_play_reply failed')

        cfg = ReceptionistConfig.objects.filter(tenant=tenant).select_related('tenant').first() if tenant else None

        # 2. Spam / automated mail
        is_spam = is_bulk or AITriageService.is_spam_or_autoreply(subject, message, sender_email)
        if cfg and not cfg.enable_spam_filter and not is_bulk:
            is_spam = False

        # 3. Triage + enrichment
        enriched = AITriageService.extract_signature_details(message, sender_name, sender_email)
        final_role = role or enriched.get("role") or ""
        final_company = company_name or enriched.get("company") or ""
        final_phone = phone_number or enriched.get("phone") or ""
        triage = AITriageService.analyze_lead_intent(subject=subject, message=message,
                                                     role=final_role, team_size=team_size)

        lead = cls.find_thread(tenant, sender_email, in_reply_to, references) if not is_spam else None
        created = lead is None
        now = timezone.now()

        if created:
            lead = ReceptionistLead.objects.create(
                tenant=tenant,
                sender_name=sender_name or sender_email or "Unknown Sender",
                sender_email=sender_email or None,
                phone_number=final_phone,
                company_name=final_company,
                role=final_role,
                team_size=team_size or "",
                est_budget=est_budget or "",
                source=source,
                subject=subject[:512],
                inbound_message=message,
                qualification_score=0 if is_spam else triage['qualification_score'],
                intent_category="System Notification" if is_spam else triage['intent'],
                fit_reason="Automated / bulk email" if is_spam else triage.get('fit_reason', ''),
                enriched_profile=enriched,
                is_spam=is_spam,
                status='archived' if is_spam else 'awaiting',
            )
        else:
            lead.inbound_message = message
            lead.qualification_score = max(lead.qualification_score or 0, triage['qualification_score'])
            lead.intent_category = triage['intent']
            lead.fit_reason = triage.get('fit_reason', '')
            if final_phone and not lead.phone_number:
                lead.phone_number = final_phone
            if final_company and not lead.company_name:
                lead.company_name = final_company
            lead.status = 'awaiting'
            lead.scheduled_send_at = None
            lead.save()

        # 4. Store the inbound message
        ReceptionistEmailMessage.objects.create(
            lead=lead,
            direction='inbound',
            sender=sender_email or 'web-form',
            recipient=recipient or (getattr(cfg, 'mailbox_connected_email', '') or ''),
            subject=subject[:512],
            body_text=message,
            sent_at=now,
            message_id=(message_id or '')[:512],
            in_reply_to=(in_reply_to or '')[:512],
            references=references or '',
            provider_message_id=(provider_message_id or '')[:512],
            **({'organization': tenant} if tenant is not None and hasattr(ReceptionistEmailMessage, 'organization') else {}),
        )

        if is_spam:
            lead.ai_draft_response = ''
            lead.save(update_fields=['ai_draft_response', 'updated_at'])
            return lead, created, True

        # 5a. Rules by product / topic: a thread keeps the rule it matched first
        from email_receptionist.services import rules_service
        rule = rules_service.find_rule(cfg, lead.rule_id) if lead.rule_id else None
        if rule is None:
            rule, _how = rules_service.match(cfg, subject=subject, body=message, sender_email=sender_email)
            if rule:
                lead.rule_id, lead.rule_name = rule['id'], rule['name']
                if rule.get('action') == 'reply':
                    lead.cc_emails = ', '.join(rule.get('cc') or [])
                lead.save(update_fields=['rule_id', 'rule_name', 'cc_emails', 'updated_at'])

        # 5. AI reply with the full thread as context (and the rule's pitch / template)
        decision = AIDraftService.generate_reply(
            sender_name=lead.sender_name,
            company_name=lead.company_name,
            intent=lead.intent_category,
            inbound_message=message,
            config=cfg,
            history=build_history(lead),
            pitch=rules_service.pitch_for(rule),
            template_key=(rule or {}).get('template_key') or None,
        )
        action = decision.get('action') or 'review'
        lead.ai_action = action
        lead.ai_draft_response = decision['reply']
        lead.escalation_reason = decision.get('reason', '') if action != 'reply' else ''

        # 5b. The AI recognised automated / transactional mail the keyword filter missed:
        #     file it like spam - no reply, no escalation alert.
        if action == 'ignore' and not created:
            # automated message inside a real conversation (e.g. an out-of-office): keep the thread, just don't answer
            lead.status = 'replied' if lead.replied_at else 'awaiting'
            lead.save(update_fields=['ai_action', 'ai_draft_response', 'escalation_reason', 'status', 'updated_at'])
            return lead, created, True
        if action == 'ignore':
            lead.is_spam = True
            lead.status = 'archived'
            lead.intent_category = 'System Notification'
            lead.qualification_score = 0
            lead.scheduled_send_at = None
            lead.save(update_fields=['ai_action', 'ai_draft_response', 'escalation_reason', 'is_spam', 'status',
                                     'intent_category', 'qualification_score', 'scheduled_send_at', 'updated_at'])
            return lead, created, True

        lead.save(update_fields=['ai_action', 'ai_draft_response', 'escalation_reason', 'updated_at'])

        # 5c. Routing Rules handling:
        if rule:
            action_type = rule.get('action')
            if action_type == 'human_review':
                # Force human review: stops any autonomous send and flags for human
                lead.status = 'awaiting'
                lead.scheduled_send_at = None
                lead.escalation_reason = f'Needs Human Intervention (Matched rule: {rule["name"]})'
                lead.save(update_fields=['status', 'scheduled_send_at', 'escalation_reason', 'updated_at'])
                return lead, created, False

            elif action_type == 'forward':
                from email_receptionist.services.email_dispatch_service import EmailDispatchService
                via = EmailDispatchService.forward_email(
                    cfg, lead, rule.get('forward_to') or [],
                    note=f'Forwarded by Email Receptionist (rule: {rule["name"]}).')
                to_text = ', '.join(rule.get('forward_to') or [])
                lead.forwarded_to = to_text if via != 'failed' else ''
                if via == 'failed':
                    lead.escalation_reason = f'Could not forward to {to_text} - check the mailbox connection.'
                if not rule.get('ack_sender'):
                    lead.status = 'forwarded' if via != 'failed' else 'awaiting'
                    lead.scheduled_send_at = None
                    lead.save(update_fields=['forwarded_to', 'escalation_reason', 'status', 'scheduled_send_at', 'updated_at'])
                    return lead, created, False
                lead.save(update_fields=['forwarded_to', 'escalation_reason', 'updated_at'])

        # 6. Autonomous send / escalate / wait for review
        AutoReplyService.maybe_schedule(lead, cfg, decision)
        return lead, created, False
