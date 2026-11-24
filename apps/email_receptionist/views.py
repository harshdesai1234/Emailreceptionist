"""
Email Receptionist views.

Every endpoint is scoped to request.organization: a user can only see or act on
leads and settings belonging to their own organization.
"""
import hmac
import json
import logging
import os
import secrets
import urllib.parse
from datetime import timedelta, datetime

from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse, Http404
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from email_receptionist.models import ReceptionistLead, ReceptionistConfig, generate_webhook_token
from email_receptionist.services.ai_draft_service import AIDraftService, build_history
from email_receptionist.services.auto_reply_service import AutoReplyService
from email_receptionist.services.calendar_service import CalendarService
from email_receptionist.services.email_dispatch_service import EmailDispatchService
from email_receptionist.services.ingestion_service import IngestionService
from email_receptionist.services.template_service import TemplateService

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _org(request):
    return getattr(request, 'organization', None)


def _json_body(request):
    try:
        data = json.loads(request.body or b'{}')
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _no_org():
    return JsonResponse({'status': 'error', 'message': 'No active organization for this user.'}, status=403)


def _get_org_lead(request, lead_id):
    """Returns the lead only if it belongs to the user's organization, else raises Http404."""
    org = _org(request)
    try:
        lead_id = int(lead_id)
    except (TypeError, ValueError):
        raise Http404('Invalid lead id')
    lead = ReceptionistLead.objects.filter(id=lead_id, tenant=org).select_related('tenant').first() if org else None
    if not lead:
        raise Http404('Lead not found')
    return lead


def _to_int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _parse_time(value, default):
    try:
        return datetime.strptime(str(value).strip()[:5], '%H:%M').time()
    except (TypeError, ValueError):
        return default


def _js_json(value):
    """JSON that is safe to embed inside a <script> tag (email content can contain '</script>')."""
    return (json.dumps(value).replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def _serialize_lead(l, suggested_slots):
    initials = "".join(p[0].upper() for p in (l.sender_name or '').split()[:2]) or "LD"
    tags = []
    if l.qualification_score >= 85:
        tags.append({"text": "Hot Lead" if l.lead_type == 'inbound_receptionist' else "Outbound Campaign", "class": "badge-hot"})
    tags.append({"text": (l.intent_category or l.source), "class": "badge-source"})
    status_tag = {
        'forwarded': ("Forwarded", "badge-parsed"),
        'replied': ("Replied", "badge-parsed"),
        'scheduled': ("Auto-Send Scheduled", "badge-demo"),
        'escalated': ("Escalated", "badge-hot"),
        'archived': ("Archived", "badge-source"),
    }.get(l.status, ("Awaiting Action", "badge-demo"))
    tags.append({"text": status_tag[0], "class": status_tag[1]})

    if l.rule_name:
        tags.append({"text": f"🔀 {l.rule_name}", "class": "badge-hot" if "urgent" in l.rule_name.lower() else "badge-source"})

    category = ("escalated" if l.status == 'escalated'
                else "replied" if l.status == 'replied'
                else "hot" if l.qualification_score >= 85 else "awaiting")
    return {
        "id": l.id,
        "mode": "receptionist" if l.lead_type == 'inbound_receptionist' else "agent",
        "name": l.sender_name,
        "avatar": initials,
        "role": l.role or "—",
        "company": l.company_name or "—",
        "email": l.sender_email or "",
        "phone": l.phone_number or "N/A",
        "size": l.team_size or "—",
        "budget": l.est_budget or "—",
        "source": l.source,
        "time": timezone.localtime(l.created_at).strftime("%b %d, %I:%M %p"),
        "subject": l.subject,
        "body": l.inbound_message,
        "score": l.qualification_score,
        "intent": l.intent_category,
        "fit_reason": l.fit_reason or "",
        "category": category,
        "status": l.status,
        "tags": tags,
        "draft": l.ai_draft_response or "",
        "escalation_reason": l.escalation_reason or "",
        "rule_name": l.rule_name or "",
        "cc_emails": l.cc_emails or "",
        "forwarded_to": l.forwarded_to or "",
        "scheduled_send_at": l.scheduled_send_at.isoformat() if l.scheduled_send_at else None,
        "auto_reply_count": l.auto_reply_count,
        "sequence_step": l.sequence_step,
        "slots": suggested_slots,
        "origin_city": l.origin_city or "",
        "destination_city": l.destination_city or "",
        "cargo_type": l.cargo_type or "",
        "weight_or_volume": l.weight_or_volume or "",
        "load_type": l.load_type or "FTL",
        "pickup_timing": l.pickup_timing or "",
        "tracking_number": l.tracking_number or "",
        "est_freight_value": float(l.est_freight_value) if l.est_freight_value is not None else None,
    }


# ── Dashboard ──────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    org = _org(request)
    config = ReceptionistConfig.objects.get_or_create(tenant=org)[0] if org else None
    if config and not config.webhook_token:
        config.webhook_token = generate_webhook_token()
        config.save(update_fields=['webhook_token'])

    base_qs = ReceptionistLead.objects.filter(tenant=org) if org else ReceptionistLead.objects.none()
    rec_qs = base_qs.filter(lead_type='inbound_receptionist').exclude(is_spam=True).exclude(status='archived').order_by('-updated_at')
    agent_qs = base_qs.filter(lead_type='outbound_agent').exclude(is_spam=True).exclude(status='archived').order_by('-updated_at')

    total_inbound = rec_qs.count()
    rec_awaiting = rec_qs.filter(status__in=['awaiting', 'scheduled']).count()
    rec_escalated = rec_qs.filter(status='escalated').count()
    rec_replied = rec_qs.filter(status='replied').count()
    auto_replied = rec_qs.filter(auto_reply_count__gt=0).count()
    total_agent = agent_qs.count()
    agent_replied = agent_qs.filter(status='replied').count()

    suggested_slots = CalendarService.get_suggested_demo_slots()
    templates = TemplateService.get_templates_for_category("all", config=config)

    context = {
        "page_title": "Email Receptionist",
        "config": config,
        "total_inbound": total_inbound,
        "awaiting_count": rec_awaiting,
        "escalated_count": rec_escalated,
        "replied_count": rec_replied,
        "auto_handled_rate": f"{round(auto_replied / total_inbound * 100, 1)}%" if total_inbound else "0%",
        "total_agent": total_agent,
        "agent_awaiting": agent_qs.filter(status='awaiting').count(),
        "agent_reply_rate": f"{round(agent_replied / total_agent * 100, 1)}%" if total_agent else "0%",
        "receptionist_leads_json": _js_json([_serialize_lead(l, suggested_slots) for l in rec_qs[:100]]),
        "agent_leads_json": _js_json([_serialize_lead(l, suggested_slots) for l in agent_qs[:100]]),
        "suggested_slots": suggested_slots,
        "templates": templates,
        "templates_json": _js_json(templates),
        "reply_rules_json": _js_json(config.reply_rules if config and config.reply_rules else []),
        "webhook_url": request.build_absolute_uri(reverse('email_receptionist:api_inbound_webhook')),
        "timezone_choices": ['Asia/Kolkata', 'UTC', 'Europe/London', 'Europe/Berlin', 'America/New_York',
                             'America/Chicago', 'America/Los_Angeles', 'Asia/Dubai', 'Asia/Singapore', 'Australia/Sydney'],
    }
    return render(request, "email_receptionist/dashboard.html", context)


# ── Lead actions ───────────────────────────────────────────────────────────

@login_required
@require_POST
def api_generate_draft(request):
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    cfg = ReceptionistConfig.objects.filter(tenant=org).select_related('tenant').first()

    lead = _get_org_lead(request, data['lead_id']) if data.get('lead_id') else None
    if lead:  # always trust stored lead data over what the browser sends
        name, company, intent, message = lead.sender_name, lead.company_name, lead.intent_category, lead.inbound_message
    else:
        name, company = data.get('name') or 'there', data.get('company') or ''
        intent, message = data.get('intent') or '', data.get('message') or ''

    from email_receptionist.services import rules_service
    rule = rules_service.find_rule(cfg, lead.rule_id) if lead and lead.rule_id else None
    decision = AIDraftService.generate_reply(
        sender_name=name, company_name=company, intent=intent, inbound_message=message,
        config=cfg, history=build_history(lead) if lead else [],
        tone=data.get('tone') or None, role_mode=data.get('role_mode', 'receptionist'),
        template_key=data.get('template_key') or (rule or {}).get('template_key') or None,
        pitch=rules_service.pitch_for(rule),
    )
    if lead:
        lead.ai_draft_response = decision['reply']
        update_fields = ['ai_draft_response', 'updated_at']
        logistics_data = decision.get('logistics_deal')
        if isinstance(logistics_data, dict):
            if logistics_data.get('origin_city'):
                lead.origin_city = str(logistics_data['origin_city'])[:128]
                update_fields.append('origin_city')
            if logistics_data.get('destination_city'):
                lead.destination_city = str(logistics_data['destination_city'])[:128]
                update_fields.append('destination_city')
            if logistics_data.get('cargo_type'):
                lead.cargo_type = str(logistics_data['cargo_type'])[:255]
                update_fields.append('cargo_type')
            if logistics_data.get('weight_or_volume'):
                lead.weight_or_volume = str(logistics_data['weight_or_volume'])[:128]
                update_fields.append('weight_or_volume')
            if logistics_data.get('load_type'):
                lead.load_type = str(logistics_data['load_type'])[:32]
                update_fields.append('load_type')
            if logistics_data.get('pickup_timing'):
                lead.pickup_timing = str(logistics_data['pickup_timing'])[:128]
                update_fields.append('pickup_timing')
            if logistics_data.get('tracking_number'):
                lead.tracking_number = str(logistics_data['tracking_number'])[:128]
                update_fields.append('tracking_number')
            if logistics_data.get('est_freight_value') is not None:
                try:
                    lead.est_freight_value = float(logistics_data['est_freight_value'])
                    update_fields.append('est_freight_value')
                except (ValueError, TypeError):
                    pass
        lead.save(update_fields=list(set(update_fields)))
    return JsonResponse({"status": "success", "draft": decision['reply'], "action": decision['action'],
                         "reason": decision.get('reason', ''), "source": decision.get('source', ''),
                         "logistics_deal": decision.get('logistics_deal', {}),
                         "slots": CalendarService.get_suggested_demo_slots()})


@login_required
@require_POST
def api_send_reply(request):
    if not _org(request):
        return _no_org()
    data = _json_body(request)
    lead = _get_org_lead(request, data.get('lead_id'))
    draft = (data.get('draft') or '').strip()
    if not draft:
        return JsonResponse({"status": "error", "message": "Reply is empty."}, status=400)

    # A manual send replaces any pending autonomous send
    ReceptionistLead.objects.filter(id=lead.id).update(scheduled_send_at=None)
    lead.refresh_from_db()

    cc = None
    if 'cc' in data:
        from email_receptionist.services.rules_service import _emails
        cc = _emails(data.get('cc') or '')
        bad = []
        for addr in cc:
            try:
                validate_email(addr)
            except ValidationError:
                bad.append(addr)
        if bad:
            return JsonResponse({"status": "error", "message": f"Not a valid CC address: {', '.join(bad[:3])}"}, status=400)
        if ', '.join(cc) != (lead.cc_emails or ''):
            lead.cc_emails = ', '.join(cc)
            lead.save(update_fields=['cc_emails', 'updated_at'])

    ok = EmailDispatchService.dispatch_receptionist_reply(
        lead=lead, draft_text=draft, selected_slot=data.get('selected_slot') or None, subject=data.get('subject'),
        cc=cc,
    )
    if ok:
        lead.auto_reply_count = 0  # a human replied - reset the autonomous safety counter
        lead.save(update_fields=['auto_reply_count', 'updated_at'])
        return JsonResponse({"status": "success", "message": f"Reply sent to {lead.sender_email}"})
    return JsonResponse({"status": "error", "message": lead.escalation_reason or "Sending failed."}, status=502)


@login_required
@require_POST
def api_cancel_auto_send(request):
    if not _org(request):
        return _no_org()
    lead = _get_org_lead(request, _json_body(request).get('lead_id'))
    if AutoReplyService.cancel(lead):
        return JsonResponse({"status": "success", "message": "Auto-send cancelled. The draft is waiting for you."})
    return JsonResponse({"status": "error", "message": "This reply is no longer scheduled (it may have been sent)."}, status=409)


@login_required
@require_POST
def api_escalate_lead(request):
    if not _org(request):
        return _no_org()
    lead = _get_org_lead(request, _json_body(request).get('lead_id'))
    lead.status = 'escalated'
    lead.scheduled_send_at = None
    lead.escalation_reason = f'Escalated by {request.user.get_full_name() or request.user.get_username()}'
    lead.save(update_fields=['status', 'scheduled_send_at', 'escalation_reason', 'updated_at'])
    via = AutoReplyService.notify_escalation(lead, reason=lead.escalation_reason)
    msg = {'mailbox': 'Escalated. The team was alerted from your connected mailbox.',
           'platform': 'Escalated. The team was alerted by email.',
           'no_recipient': 'Escalated. No alert was sent: add an address under "Send escalation alerts to".',
           }.get(via, 'Escalated, but the alert email could not be sent (check the mailbox connection).')
    return JsonResponse({"status": "success", "message": msg, "alert": via})


@login_required
@require_POST
def api_archive_lead(request):
    if not _org(request):
        return _no_org()
    lead = _get_org_lead(request, _json_body(request).get('lead_id'))
    lead.status = 'archived'
    lead.scheduled_send_at = None
    lead.save(update_fields=['status', 'scheduled_send_at', 'updated_at'])
    return JsonResponse({"status": "success", "message": "Lead archived."})


@login_required
def api_get_lead_messages(request):
    if not _org(request):
        return _no_org()
    lead = _get_org_lead(request, request.GET.get('lead_id'))
    serialized = [{
        'id': m.id, 'direction': m.direction, 'sender': m.sender, 'recipient': m.recipient,
        'subject': m.subject, 'body': m.body_text[:4000],
        'sent_at': timezone.localtime(m.sent_at).strftime('%b %d, %I:%M %p'),
    } for m in lead.messages.order_by('sent_at')]
    return JsonResponse({'status': 'success', 'messages': serialized, 'lead_id': lead.id,
                         'lead_status': lead.status,
                         'scheduled_send_at': lead.scheduled_send_at.isoformat() if lead.scheduled_send_at else None})


# ── Test tools ─────────────────────────────────────────────────────────────

@login_required
@require_POST
def api_preview_reply(request):
    """'Try a sample email': shows what the AI would reply. Nothing is sent or saved."""
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    message = str(data.get('message') or '').strip()
    if not message:
        return JsonResponse({"status": "error", "message": "Type a sample customer email first."}, status=400)
    from email_receptionist.services.ai_triage_service import AITriageService
    from email_receptionist.services.ai_draft_service import configured_providers

    cfg = ReceptionistConfig.objects.filter(tenant=org).select_related('tenant').first()
    subject = str(data.get('subject') or 'Sample inquiry')[:512]
    sender_name = str(data.get('name') or 'Test Customer')[:255]
    is_spam = AITriageService.is_spam_or_autoreply(subject, message, '')
    triage = AITriageService.analyze_lead_intent(subject=subject, message=message)
    from email_receptionist.services import rules_service
    rule, how = rules_service.match(cfg, subject, message) if cfg else (None, '')
    decision = AIDraftService.generate_reply(
        sender_name=sender_name, company_name='', intent=triage['intent'], inbound_message=message[:10000],
        config=cfg, tone=data.get('tone') or None, pitch=rules_service.pitch_for(rule),
        template_key=(rule or {}).get('template_key') or None,
    )
    is_spam = is_spam or decision['action'] == 'ignore'
    forward_only = bool(rule and rule.get('action') == 'forward' and not rule.get('ack_sender'))
    would_auto_send = bool(
        cfg and cfg.is_autonomous_mode and decision['action'] == 'reply' and not is_spam and not forward_only
        and AutoReplyService.is_within_coverage(cfg)
    )
    return JsonResponse({
        "status": "success",
        "reply": decision['reply'], "action": decision['action'], "reason": decision.get('reason', ''),
        "source": decision.get('source', ''), "intent": triage['intent'], "score": triage['qualification_score'],
        "is_spam": is_spam, "would_auto_send": would_auto_send,
        "autonomous_mode": bool(cfg and cfg.is_autonomous_mode),
        "providers": configured_providers(),
        "rule": ({"name": rule['name'], "action": rule['action'], "matched_by": how, "cc": rule.get('cc') or [],
                  "forward_to": rule.get('forward_to') or [], "ack_sender": bool(rule.get('ack_sender'))}
                 if rule else None),
    })


@login_required
@require_POST
def api_send_test_email(request):
    """'Send test email': sends one email from the connected mailbox to check that sending works."""
    org = _org(request)
    if not org:
        return _no_org()
    to_email = str(_json_body(request).get('to_email') or request.user.email or '').strip()
    try:
        validate_email(to_email)
    except ValidationError:
        return JsonResponse({"status": "error", "message": "Enter a valid email address to send the test to."}, status=400)
    cfg = ReceptionistConfig.objects.filter(tenant=org).select_related('tenant').first()
    ok, msg = EmailDispatchService.send_test_email(cfg, to_email)
    return JsonResponse({"status": "success" if ok else "error", "message": msg}, status=200 if ok else 400)


# ── Settings ───────────────────────────────────────────────────────────────

@login_required
@require_POST
def api_save_config(request):
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    was_autonomous = cfg.is_autonomous_mode

    if 'reply_rules' in data:
        from email_receptionist.services import rules_service
        rules, errors = rules_service.clean_rules(data.get('reply_rules') or [])
        if errors:
            return JsonResponse({"status": "error", "message": '; '.join(errors[:5])}, status=400)
        cfg.reply_rules = rules

    if 'escalation_emails' in data:
        raw = str(data['escalation_emails'] or '')
        parts = [p.strip() for p in raw.replace(';', ',').replace('\n', ',').split(',') if p.strip()]
        bad = []
        for addr in parts:
            try:
                validate_email(addr)
            except ValidationError:
                bad.append(addr)
        if bad:
            return JsonResponse({"status": "error", "message": f"Not a valid email address: {', '.join(bad[:3])}"}, status=400)
        cfg.escalation_emails = ', '.join(dict.fromkeys(parts))[:2000]

    if 'is_autonomous_mode' in data:
        cfg.is_autonomous_mode = bool(data['is_autonomous_mode'])
        if not cfg.is_autonomous_mode:
            # Turning autonomous mode off cancels everything still waiting to be sent
            ReceptionistLead.objects.filter(tenant=org, status='scheduled').update(
                status='awaiting', scheduled_send_at=None,
                escalation_reason='Autonomous mode turned off.', updated_at=timezone.now())
    if data.get('default_tone') in dict(ReceptionistConfig.TONE_CHOICES):
        cfg.default_tone = data['default_tone']
    if data.get('coverage_schedule') in dict(ReceptionistConfig.SCHEDULE_CHOICES):
        cfg.coverage_schedule = data['coverage_schedule']
    if data.get('business_type') in dict(ReceptionistConfig.BUSINESS_TYPE_CHOICES):
        cfg.business_type = data['business_type']
    if 'business_hours_start' in data:
        cfg.business_hours_start = _parse_time(data['business_hours_start'], cfg.business_hours_start)
    if 'business_hours_end' in data:
        cfg.business_hours_end = _parse_time(data['business_hours_end'], cfg.business_hours_end)
    if 'business_timezone' in data:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(data['business_timezone']))
            cfg.business_timezone = str(data['business_timezone'])
        except Exception:
            return JsonResponse({"status": "error", "message": "Unknown timezone."}, status=400)
    if 'auto_reply_delay_seconds' in data:
        cfg.auto_reply_delay_seconds = _to_int(data['auto_reply_delay_seconds'], cfg.auto_reply_delay_seconds, 0, 3600)
    if 'max_auto_replies_per_thread' in data:
        cfg.max_auto_replies_per_thread = _to_int(data['max_auto_replies_per_thread'], cfg.max_auto_replies_per_thread, 0, 50)
    for text_field in ('company_faq_guardrails', 'company_tagline', 'services_offered',
                       'pricing_model_details', 'call_to_action_type', 'mailbox_display_name',
                       'raw_business_notes'):
        if text_field in data:
            setattr(cfg, text_field, str(data[text_field] or '').strip())
    if 'extracted_knowledge' in data and isinstance(data['extracted_knowledge'], dict):
        cfg.extracted_knowledge = data['extracted_knowledge']
    if 'knowledge_chat_history' in data and isinstance(data['knowledge_chat_history'], list):
        cfg.knowledge_chat_history = data['knowledge_chat_history']
    if 'booking_calendar_url' in data:
        cfg.booking_calendar_url = str(data['booking_calendar_url'] or '').strip() or None
    if 'enable_spam_filter' in data:
        cfg.enable_spam_filter = bool(data['enable_spam_filter'])
    if isinstance(data.get('custom_templates'), list):
        cfg.custom_templates = [t for t in data['custom_templates'] if isinstance(t, dict)][:50]

    # Mailbox connection
    if data.get('email_provider') in dict(ReceptionistConfig.PROVIDER_CHOICES):
        cfg.email_provider = data['email_provider']
    if 'mailbox_connected_email' in data:
        addr = str(data['mailbox_connected_email'] or '').strip()
        if addr:
            try:
                validate_email(addr)
            except ValidationError:
                return JsonResponse({"status": "error", "message": "Invalid mailbox email address."}, status=400)
        cfg.mailbox_connected_email = addr or None
        cfg.gmail_connected_email = cfg.mailbox_connected_email
    if data.get('mailbox_app_password'):
        cfg.mailbox_app_password = str(data['mailbox_app_password']).strip()
        cfg.gmail_app_password = None  # legacy copy no longer needed
    if 'mailbox_sync_enabled' in data:
        cfg.mailbox_sync_enabled = bool(data['mailbox_sync_enabled'])
        cfg.gmail_sync_enabled = cfg.mailbox_sync_enabled
    for host_field in ('imap_host', 'smtp_host'):
        if host_field in data:
            setattr(cfg, host_field, str(data[host_field] or '').strip() or None)
    if 'imap_port' in data:
        cfg.imap_port = _to_int(data['imap_port'], cfg.imap_port, 1, 65535)
    if 'smtp_port' in data:
        cfg.smtp_port = _to_int(data['smtp_port'], cfg.smtp_port, 1, 65535)
    if 'use_ssl_tls' in data:
        cfg.use_ssl_tls = bool(data['use_ssl_tls'])

    cfg.save()

    # Switched ON just now: also answer the emails already waiting (last 24h) that the AI judged safe.
    scheduled_now = AutoReplyService.schedule_pending(cfg) if (cfg.is_autonomous_mode and not was_autonomous) else 0
    message = "Settings saved."
    if scheduled_now:
        message += f" {scheduled_now} waiting email(s) will now be answered automatically."
    return JsonResponse({
        "status": "success", "message": message, "scheduled_now": scheduled_now,
        "connected_email": cfg.mailbox_connected_email, "provider": cfg.email_provider,
        "sync_enabled": cfg.mailbox_sync_enabled, "is_autonomous_mode": cfg.is_autonomous_mode,
    })


@login_required
@require_POST
def api_regenerate_webhook_token(request):
    org = _org(request)
    if not org:
        return _no_org()
    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    cfg.webhook_token = generate_webhook_token()
    cfg.save(update_fields=['webhook_token'])
    return JsonResponse({"status": "success", "webhook_token": cfg.webhook_token})


# ── Interactive Knowledge Agent API ────────────────────────────────────────

@login_required
@require_POST
def api_knowledge_agent_chat(request):
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    user_msg = str(data.get('message') or '').strip()
    if not user_msg:
        return JsonResponse({"status": "error", "message": "Message cannot be empty."}, status=400)

    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    history = list(cfg.knowledge_chat_history or [])
    notes = cfg.raw_business_notes or ''

    from email_receptionist.services.business_knowledge_agent_service import BusinessKnowledgeAgentService

    # Append user message
    history.append({"role": "user", "text": user_msg})

    res = BusinessKnowledgeAgentService.chat_with_agent(
        user_message=user_msg,
        chat_history=history,
        current_notes=notes,
        company_name=getattr(org, 'name', '')
    )
    agent_reply = res.get('reply', '')
    history.append({"role": "agent", "text": agent_reply})

    cfg.knowledge_chat_history = history[-30:]
    cfg.save(update_fields=['knowledge_chat_history'])

    return JsonResponse({
        "status": "success",
        "reply": agent_reply,
        "history": cfg.knowledge_chat_history,
    })


@login_required
@require_POST
def api_synthesize_knowledge(request):
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    notes = str(data.get('notes') or '').strip()

    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    if notes:
        cfg.raw_business_notes = notes

    from email_receptionist.services.business_knowledge_agent_service import BusinessKnowledgeAgentService

    extracted = BusinessKnowledgeAgentService.synthesize_knowledge(
        notes=cfg.raw_business_notes,
        chat_history=cfg.knowledge_chat_history,
        company_name=getattr(org, 'name', '')
    )

    # Automatically map to configuration fields
    if extracted.get('business_type') in dict(ReceptionistConfig.BUSINESS_TYPE_CHOICES):
        cfg.business_type = extracted['business_type']
    if extracted.get('call_to_action_type'):
        cfg.call_to_action_type = extracted['call_to_action_type']
    if extracted.get('company_tagline'):
        cfg.company_tagline = extracted['company_tagline']
    if extracted.get('services_offered'):
        cfg.services_offered = extracted['services_offered']
    if extracted.get('pricing_model_details'):
        cfg.pricing_model_details = extracted['pricing_model_details']
    if extracted.get('company_faq_guardrails'):
        cfg.company_faq_guardrails = extracted['company_faq_guardrails']
    if extracted.get('tone_recommendation') in dict(ReceptionistConfig.TONE_CHOICES):
        cfg.default_tone = extracted['tone_recommendation']

    cfg.extracted_knowledge = extracted
    cfg.save()

    return JsonResponse({
        "status": "success",
        "message": "Business knowledge extracted and applied successfully!",
        "extracted": extracted,
        "config": {
            "business_type": cfg.business_type,
            "company_tagline": cfg.company_tagline,
            "services_offered": cfg.services_offered,
            "pricing_model_details": cfg.pricing_model_details,
            "company_faq_guardrails": cfg.company_faq_guardrails,
            "call_to_action_type": cfg.call_to_action_type,
            "default_tone": cfg.default_tone,
            "raw_business_notes": cfg.raw_business_notes,
        }
    })


@login_required
@require_POST
def api_clear_knowledge_chat(request):
    org = _org(request)
    if not org:
        return _no_org()
    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    cfg.knowledge_chat_history = []
    cfg.save(update_fields=['knowledge_chat_history'])
    return JsonResponse({"status": "success", "message": "Knowledge conversation reset."})


@login_required
@require_POST
def api_scrape_knowledge_website(request):
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    url = str(data.get('url') or '').strip()
    if not url:
        return JsonResponse({"status": "error", "message": "Website URL is required."}, status=400)
    
    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url

    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')
        # Remove script, style, nav, and footer tags
        for el in soup(['script', 'style', 'nav', 'footer', 'noscript', 'header']):
            el.decompose()

        text = ' '.join(soup.stripped_strings)
        text = text[:15000].strip() # Limit to first 15k chars

        if not text:
            return JsonResponse({"status": "error", "message": "Could not extract text from this website."}, status=400)

        cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
        header = f"\n\n--- Website: {url} ---\n"
        current_notes = cfg.raw_business_notes or ""
        cfg.raw_business_notes = (current_notes + header + text).strip()
        cfg.save(update_fields=['raw_business_notes'])

        return JsonResponse({
            "status": "success",
            "message": f"Learned from {url}! ({len(text)} characters extracted)",
            "url": url,
            "extracted_text": text,
            "full_notes": cfg.raw_business_notes
        })
    except Exception as e:
        logger.warning(f"Error scraping website {url}: {e}")
@login_required
@require_POST
def api_upload_knowledge_document(request):
    org = _org(request)
    if not org:
        return _no_org()
    
    file_obj = request.FILES.get('document')
    if not file_obj:
        return JsonResponse({"status": "error", "message": "No file uploaded."}, status=400)
    
    name = file_obj.name.lower()
    text = ""
    
    try:
        if name.endswith('.txt') or name.endswith('.md') or name.endswith('.csv'):
            text = file_obj.read().decode('utf-8', errors='replace')
        elif name.endswith('.pdf'):
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_obj.read()))
            extracted_pages = []
            for i, page in enumerate(reader.pages[:30]):  # limit up to first 30 pages
                page_text = page.extract_text()
                if page_text:
                    extracted_pages.append(page_text)
            text = "\n\n".join(extracted_pages)
        else:
            return JsonResponse({"status": "error", "message": "Unsupported file format. Please upload a .pdf, .txt, or .md file."}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Error parsing document: {str(e)}"}, status=400)
    
    text = text.strip()
    if not text:
        return JsonResponse({"status": "error", "message": "The uploaded document contains no extractable text."}, status=400)
    
    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    current_notes = cfg.raw_business_notes or ""
    header = f"\n\n--- Document: {file_obj.name} ---\n"
    cfg.raw_business_notes = (current_notes + header + text).strip()

    # Automatically structure knowledge from the uploaded PDF
    from email_receptionist.services.business_knowledge_agent_service import BusinessKnowledgeAgentService
    extracted = BusinessKnowledgeAgentService.synthesize_knowledge(
        notes=cfg.raw_business_notes,
        chat_history=cfg.knowledge_chat_history,
        company_name=getattr(org, 'name', '')
    )
    if extracted.get('business_type') in dict(ReceptionistConfig.BUSINESS_TYPE_CHOICES):
        cfg.business_type = extracted['business_type']
    if extracted.get('call_to_action_type'):
        cfg.call_to_action_type = extracted['call_to_action_type']
    if extracted.get('company_tagline'):
        cfg.company_tagline = extracted['company_tagline']
    if extracted.get('services_offered'):
        cfg.services_offered = extracted['services_offered']
    if extracted.get('pricing_model_details'):
        cfg.pricing_model_details = extracted['pricing_model_details']
    if extracted.get('company_faq_guardrails'):
        cfg.company_faq_guardrails = extracted['company_faq_guardrails']
    if extracted.get('tone_recommendation') in dict(ReceptionistConfig.TONE_CHOICES):
        cfg.default_tone = extracted['tone_recommendation']

    cfg.extracted_knowledge = extracted
    cfg.save()
    
    return JsonResponse({
        "status": "success",
        "message": f"Extracted {len(text)} characters from {file_obj.name} and structured business knowledge!",
        "filename": file_obj.name,
        "extracted_text": text,
        "full_notes": cfg.raw_business_notes,
        "extracted": extracted,
        "config": {
            "services_offered": cfg.services_offered,
            "pricing_model_details": cfg.pricing_model_details,
            "company_faq_guardrails": cfg.company_faq_guardrails,
        }
    })




# ── Mailbox ────────────────────────────────────────────────────────────────

@login_required
@require_POST
def api_poll_mailbox(request):
    org = _org(request)
    if not org:
        return _no_org()
    cfg = ReceptionistConfig.objects.filter(tenant=org).first()
    if not cfg:
        return JsonResponse({'status': 'error', 'message': 'Please configure your mailbox first.'}, status=400)
    from email_receptionist.services.imap_poller_service import IMAPPollerService
    if not IMAPPollerService.acquire_lock(cfg):  # same lock as the background poller
        return JsonResponse({'status': 'success', 'ingested': 0, 'skipped_spam': 0, 'errors': [],
                             'message': 'Inbox sync already running.'})
    try:
        result = IMAPPollerService.poll(tenant=org, config=cfg)
    finally:
        IMAPPollerService.release_lock(cfg)
    AutoReplyService.dispatch_due()
    return JsonResponse({
        'status': 'error' if result['errors'] and not result['ingested'] else 'success',
        'ingested': result['ingested'], 'skipped_spam': result['skipped_spam'],
        'errors': result['errors'], 'last_polled': result['last_polled'],
        'message': (f"{result['ingested']} new email(s) ingested." if not result['errors']
                    else '; '.join(result['errors'])),
    })


@login_required
@require_POST
def api_test_mailbox_connection(request):
    """Tests the posted credentials, or the saved connection if no new password was entered."""
    org = _org(request)
    if not org:
        return _no_org()
    data = _json_body(request)
    from email_receptionist.services.imap_poller_service import IMAPPollerService

    saved = ReceptionistConfig.objects.filter(tenant=org).first()
    if saved and not (data.get('mailbox_app_password') or '').strip():
        return JsonResponse(IMAPPollerService.test_connection(config=saved))

    class TempConfig:
        email_provider = data.get('email_provider', 'gmail')
        mailbox_connected_email = (data.get('mailbox_connected_email') or '').strip()
        mailbox_app_password = (data.get('mailbox_app_password') or '').strip()
        imap_host = (data.get('imap_host') or '').strip() or None
        imap_port = _to_int(data.get('imap_port'), 993, 1, 65535)
        use_ssl_tls = bool(data.get('use_ssl_tls', True))
        gmail_connected_email = None
        gmail_app_password = None
        google_access_token = google_refresh_token = None
        microsoft_access_token = microsoft_refresh_token = None

    return JsonResponse(IMAPPollerService.test_connection(config=TempConfig()))


# ── Public webhook (web forms / Zapier / ads) ──────────────────────────────

@csrf_exempt
@require_POST
def api_inbound_webhook(request):
    """
    POST JSON with header  X-Webhook-Token: <token from Email Receptionist settings>
    Body: {"name", "email", "company", "phone", "subject", "message"}
    """
    token = request.headers.get('X-Webhook-Token') or request.GET.get('token') or ''
    cfg = ReceptionistConfig.objects.filter(webhook_token=token).select_related('tenant').first() if token else None
    if not cfg or not cfg.webhook_token or not hmac.compare_digest(cfg.webhook_token, token):
        return JsonResponse({"status": "error", "message": "Invalid or missing webhook token."}, status=401)

    data = _json_body(request)
    sender_email = str(data.get('email') or data.get('email_address') or '').strip()
    if sender_email:
        try:
            validate_email(sender_email)
        except ValidationError:
            return JsonResponse({"status": "error", "message": "Invalid email address."}, status=400)
    message = str(data.get('message') or data.get('query') or data.get('notes') or '').strip()
    if not sender_email and not message:
        return JsonResponse({"status": "error", "message": "Provide at least an email or a message."}, status=400)

    try:
        lead, created, is_spam = IngestionService.ingest_lead_or_message(
            tenant=cfg.tenant,
            sender_name=str(data.get('name') or data.get('customer_name') or '')[:255],
            sender_email=sender_email,
            subject=str(data.get('subject') or '')[:512],
            message=message[:10000],
            phone_number=str(data.get('phone') or data.get('phone_number') or '')[:64],
            company_name=str(data.get('company') or data.get('company_name') or '')[:255],
            role=str(data.get('role') or '')[:128],
            team_size=str(data.get('team_size') or '')[:64],
            est_budget=str(data.get('budget') or '')[:64],
            source=str(data.get('source') or 'Web Form')[:128],
        )
    except Exception as e:
        logger.error(f"Inbound webhook error: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": "Could not process the lead."}, status=500)
    return JsonResponse({"status": "success", "lead_id": lead.id, "created": created, "is_spam": is_spam})


# ── OAuth (Google / Microsoft / Zoho) ──────────────────────────────────────

def _oauth_redirect_uri(request, url_name):
    scheme = 'https' if (request.is_secure() or request.headers.get('X-Forwarded-Proto') == 'https'
                         or 'ngrok' in request.get_host()) else request.scheme
    return f"{scheme}://{request.get_host()}{reverse(url_name)}"


def _oauth_start(request, provider):
    state = secrets.token_urlsafe(24)
    request.session[f'email_receptionist_oauth_state_{provider}'] = state
    return state


def _oauth_check(request, provider):
    """Validates the OAuth `state` (stops someone else's mailbox being attached to your org)."""
    expected = request.session.pop(f'email_receptionist_oauth_state_{provider}', None)
    got = request.GET.get('state') or ''
    return bool(expected) and hmac.compare_digest(expected, got)


def _oauth_setting(name):
    return getattr(settings, name, None) or os.environ.get(name)


OAUTH_FIELDS = {
    'google': ('google_access_token', 'google_refresh_token', 'google_token_expiry'),
    'microsoft': ('microsoft_access_token', 'microsoft_refresh_token', 'microsoft_token_expiry'),
    'zoho': ('zoho_access_token', 'zoho_refresh_token', 'zoho_token_expiry'),
}


def _revoke(cfg, provider):
    """Best effort: tell the provider to drop our access (Google and Zoho support it; Microsoft has no such call)."""
    import requests
    try:
        if provider == 'google' and (cfg.google_refresh_token or cfg.google_access_token):
            requests.post('https://oauth2.googleapis.com/revoke',
                          params={'token': cfg.google_refresh_token or cfg.google_access_token}, timeout=5)
        elif provider == 'zoho' and cfg.zoho_refresh_token:
            server = cfg.zoho_accounts_server or getattr(settings, 'ZOHO_ACCOUNTS_SERVER', 'https://accounts.zoho.com')
            requests.post(f'{server}/oauth/v2/token/revoke', params={'token': cfg.zoho_refresh_token}, timeout=5)
    except Exception:
        logger.warning('Revoking the %s mailbox token failed', provider, exc_info=True)


def _forget_mailbox(cfg, keep=None):
    """Drop every saved mailbox login except `keep` (the provider being connected now). Connecting a new account
    must not leave an old provider's login behind -- it would keep winning when the inbox is read."""
    for provider, fields in OAUTH_FIELDS.items():
        if provider == keep:
            continue
        _revoke(cfg, provider)
        for f in fields:
            setattr(cfg, f, None)
    if keep is None:
        cfg.mailbox_app_password = None
        cfg.gmail_app_password = None
        cfg.mailbox_connected_email = None
        cfg.gmail_connected_email = None
        cfg.mailbox_sync_enabled = False
        cfg.mailbox_last_error = ''


@login_required
@require_POST
def api_disconnect_mailbox(request):
    """Log the receptionist out of its mailbox (all sign-ins and the app password), so another one can be added."""
    org = _org(request)
    if not org:
        return _no_org()
    cfg = ReceptionistConfig.objects.filter(tenant=org).first()
    if not cfg:
        return JsonResponse({'status': 'success', 'message': 'No mailbox was connected.'})
    was = cfg.mailbox_connected_email or cfg.gmail_connected_email or ''
    _forget_mailbox(cfg)
    cfg.save()
    logger.info('Email receptionist mailbox %s disconnected for org %s by user %s', was, org.pk, request.user.pk)
    return JsonResponse({'status': 'success', 'message': f'{was or "The mailbox"} is disconnected. Connect another one any time.'})


def _callback_guard(request, provider):
    """Common callback checks. Returns (config, error_redirect)."""
    if request.GET.get('error'):
        django_messages.error(request, f'{provider.title()} authentication failed: {request.GET.get("error")}')
        return None, redirect('email_receptionist:dashboard')
    if not _oauth_check(request, provider):
        django_messages.error(request, 'Mailbox connection expired or was not started from this page. Please try again.')
        return None, redirect('email_receptionist:dashboard')
    if not request.GET.get('code'):
        django_messages.error(request, f'{provider.title()} authentication failed: missing authorization code.')
        return None, redirect('email_receptionist:dashboard')
    org = _org(request)
    if not org:
        django_messages.error(request, 'Your user is not linked to an organization.')
        return None, redirect('email_receptionist:dashboard')
    cfg, _ = ReceptionistConfig.objects.get_or_create(tenant=org)
    return cfg, None


@login_required
def google_oauth_connect(request):
    client_id = _oauth_setting('GOOGLE_OAUTH_CLIENT_ID')
    if not client_id or not _oauth_setting('GOOGLE_OAUTH_CLIENT_SECRET'):
        django_messages.error(request, 'Google OAuth (GOOGLE_OAUTH_CLIENT_ID / SECRET) is not configured.')
        return redirect('email_receptionist:dashboard')
    params = {
        'response_type': 'code', 'client_id': client_id,
        'redirect_uri': _oauth_redirect_uri(request, 'email_receptionist:google_oauth_callback'),
        'scope': ' '.join(['https://www.googleapis.com/auth/userinfo.email', 'https://mail.google.com/']),
        'access_type': 'offline', 'prompt': 'select_account consent',
        'state': _oauth_start(request, 'google'),
    }
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode(params))


@login_required
def google_oauth_callback(request):
    import requests
    cfg, err = _callback_guard(request, 'google')
    if err:
        return err
    try:
        res = requests.post('https://oauth2.googleapis.com/token', data={
            'code': request.GET['code'],
            'client_id': _oauth_setting('GOOGLE_OAUTH_CLIENT_ID'),
            'client_secret': _oauth_setting('GOOGLE_OAUTH_CLIENT_SECRET'),
            'redirect_uri': _oauth_redirect_uri(request, 'email_receptionist:google_oauth_callback'),
            'grant_type': 'authorization_code',
        }, timeout=10)
        if res.status_code != 200:
            logger.error(f'Google token exchange failed: {res.text[:500]}')
            django_messages.error(request, 'Could not connect Google account. Please try again.')
            return redirect('email_receptionist:dashboard')
        tok = res.json()
        info = requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                            headers={'Authorization': f"Bearer {tok.get('access_token')}"}, timeout=10)
        user_email = info.json().get('email') if info.status_code == 200 else None

        _forget_mailbox(cfg, keep='google')
        cfg.email_provider = 'gmail'
        cfg.google_access_token = tok.get('access_token')
        if tok.get('refresh_token'):
            cfg.google_refresh_token = tok['refresh_token']
        cfg.google_token_expiry = timezone.now() + timedelta(seconds=tok.get('expires_in', 3600))
        cfg.imap_host, cfg.imap_port, cfg.smtp_host, cfg.smtp_port = 'imap.gmail.com', 993, 'smtp.gmail.com', 587
        if user_email:
            cfg.mailbox_connected_email = user_email
        cfg.mailbox_sync_enabled = True
        cfg.save()
        django_messages.success(request, f'Connected Google account {user_email or ""}.')
    except Exception as e:
        logger.error(f'Google OAuth callback error: {e}', exc_info=True)
        django_messages.error(request, 'Google connection failed. Please try again.')
    return redirect('email_receptionist:dashboard')


@login_required
def microsoft_oauth_connect(request):
    client_id = _oauth_setting('MICROSOFT_OAUTH_CLIENT_ID')
    if not client_id:
        django_messages.error(request, 'Microsoft OAuth (MICROSOFT_OAUTH_CLIENT_ID / SECRET) is not configured.')
        return redirect('email_receptionist:dashboard')
    params = {
        'client_id': client_id, 'response_type': 'code', 'response_mode': 'query',
        'redirect_uri': _oauth_redirect_uri(request, 'email_receptionist:microsoft_oauth_callback'),
        'scope': 'offline_access User.Read Mail.ReadWrite Mail.Send',
        'prompt': 'select_account', 'state': _oauth_start(request, 'microsoft'),
    }
    return redirect('https://login.microsoftonline.com/common/oauth2/v2.0/authorize?' + urllib.parse.urlencode(params))


@login_required
def microsoft_oauth_callback(request):
    import requests
    cfg, err = _callback_guard(request, 'microsoft')
    if err:
        return err
    try:
        res = requests.post('https://login.microsoftonline.com/common/oauth2/v2.0/token', data={
            'client_id': _oauth_setting('MICROSOFT_OAUTH_CLIENT_ID'),
            'client_secret': _oauth_setting('MICROSOFT_OAUTH_CLIENT_SECRET'),
            'code': request.GET['code'],
            'redirect_uri': _oauth_redirect_uri(request, 'email_receptionist:microsoft_oauth_callback'),
            'grant_type': 'authorization_code',
        }, timeout=10)
        if res.status_code != 200:
            logger.error(f'Microsoft token exchange failed: {res.text[:500]}')
            django_messages.error(request, 'Could not connect Microsoft account. Please try again.')
            return redirect('email_receptionist:dashboard')
        tok = res.json()
        prof = requests.get('https://graph.microsoft.com/v1.0/me',
                            headers={'Authorization': f"Bearer {tok.get('access_token')}"}, timeout=10)
        user_email = None
        if prof.status_code == 200:
            p = prof.json()
            user_email = p.get('mail') or p.get('userPrincipalName')

        _forget_mailbox(cfg, keep='microsoft')
        cfg.email_provider = 'outlook'
        cfg.microsoft_access_token = tok.get('access_token')
        if tok.get('refresh_token'):
            cfg.microsoft_refresh_token = tok['refresh_token']
        cfg.microsoft_token_expiry = timezone.now() + timedelta(seconds=tok.get('expires_in', 3600))
        if user_email:
            cfg.mailbox_connected_email = user_email
        cfg.mailbox_sync_enabled = True
        cfg.save()
        django_messages.success(request, f'Connected Microsoft account {user_email or ""}.')
    except Exception as e:
        logger.error(f'Microsoft OAuth callback error: {e}', exc_info=True)
        django_messages.error(request, 'Microsoft connection failed. Please try again.')
    return redirect('email_receptionist:dashboard')


ZOHO_ACCOUNTS_SERVERS = ('https://accounts.zoho.com', 'https://accounts.zoho.in', 'https://accounts.zoho.eu',
                         'https://accounts.zoho.com.au', 'https://accounts.zoho.jp', 'https://accounts.zohocloud.ca')


@login_required
def zoho_oauth_connect(request):
    """
    NOTE: Zoho OAuth tokens are stored, but polling/sending runs through IMAP/SMTP with a
    Zoho app password. For Zoho, also save an app password in Mailbox settings.
    """
    client_id = _oauth_setting('ZOHO_OAUTH_CLIENT_ID')
    if not client_id:
        django_messages.error(request, 'Zoho OAuth is not configured. For Zoho, connect with an app password instead.')
        return redirect('email_receptionist:dashboard')
    accounts_server = getattr(settings, 'ZOHO_ACCOUNTS_SERVER', 'https://accounts.zoho.com')
    params = {
        'response_type': 'code', 'client_id': client_id,
        'scope': 'ZohoMail.messages.ALL,ZohoMail.accounts.READ,aaaserver.profile.READ',
        'redirect_uri': _oauth_redirect_uri(request, 'email_receptionist:zoho_oauth_callback'),
        'access_type': 'offline', 'prompt': 'consent', 'state': _oauth_start(request, 'zoho'),
    }
    return redirect(f"{accounts_server}/oauth/v2/auth?" + urllib.parse.urlencode(params))


@login_required
def zoho_oauth_callback(request):
    import requests
    cfg, err = _callback_guard(request, 'zoho')
    if err:
        return err
    accounts_server = request.GET.get('accounts-server') or getattr(settings, 'ZOHO_ACCOUNTS_SERVER', 'https://accounts.zoho.com')
    if accounts_server not in ZOHO_ACCOUNTS_SERVERS:
        django_messages.error(request, 'Unknown Zoho accounts server.')
        return redirect('email_receptionist:dashboard')
    try:
        res = requests.post(f"{accounts_server}/oauth/v2/token", data={
            'client_id': _oauth_setting('ZOHO_OAUTH_CLIENT_ID'),
            'client_secret': _oauth_setting('ZOHO_OAUTH_CLIENT_SECRET'),
            'code': request.GET['code'],
            'redirect_uri': _oauth_redirect_uri(request, 'email_receptionist:zoho_oauth_callback'),
            'grant_type': 'authorization_code',
        }, timeout=10)
        tok = res.json() if res.status_code == 200 else {}
        if 'access_token' not in tok:
            logger.error(f'Zoho token exchange failed: {res.text[:500]}')
            django_messages.error(request, 'Could not connect Zoho account. Please try again.')
            return redirect('email_receptionist:dashboard')
        user_email = None
        try:
            prof = requests.get(f'{accounts_server}/oauth/user/info',
                                headers={'Authorization': f"Bearer {tok['access_token']}"}, timeout=10)
            if prof.status_code == 200:
                user_email = prof.json().get('Email') or prof.json().get('email')
        except Exception:
            pass
        _forget_mailbox(cfg, keep='zoho')
        cfg.email_provider = 'zoho'
        cfg.zoho_access_token = tok.get('access_token')
        if tok.get('refresh_token'):
            cfg.zoho_refresh_token = tok['refresh_token']
        cfg.zoho_token_expiry = timezone.now() + timedelta(seconds=tok.get('expires_in', 3600))
        cfg.zoho_accounts_server = accounts_server
        if user_email:
            cfg.mailbox_connected_email = user_email
        cfg.save()
        django_messages.success(request, f'Zoho account {user_email or ""} linked. Add a Zoho app password to start syncing.')
    except Exception as e:
        logger.error(f'Zoho OAuth callback error: {e}', exc_info=True)
        django_messages.error(request, 'Zoho connection failed. Please try again.')
    return redirect('email_receptionist:dashboard')




