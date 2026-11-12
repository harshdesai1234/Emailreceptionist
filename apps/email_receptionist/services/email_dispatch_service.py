"""
EmailDispatchService — sends replies from the organization's own mailbox.

  * Microsoft OAuth  -> Microsoft Graph (replies in-thread via /messages/{id}/reply when possible)
  * Google OAuth     -> SMTP with XOAUTH2
  * App password     -> SMTP (STARTTLS 587 or SSL 465)
  * nothing set      -> Django's default email backend (DEFAULT_FROM_EMAIL)

Every reply carries In-Reply-To / References headers so it stays in the same
conversation in the customer's mail client, and gets its own Message-ID so the
customer's answer can be matched back to the thread.

A lead is only marked 'replied' when the send actually succeeded.
"""
import html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, formataddr
from typing import Optional

from django.conf import settings
from django.utils import timezone

from email_receptionist.models import ReceptionistLead, ReceptionistEmailMessage, ReceptionistConfig

logger = logging.getLogger(__name__)


def split_emails(value) -> list:
    """'a@x.com, b@y.com' / list -> ['a@x.com', 'b@y.com'] (deduplicated, order kept)."""
    import re as _re
    if not value:
        return []
    parts = _re.split(r'[,;\s]+', value) if isinstance(value, str) else list(value)
    out = []
    for p in parts:
        p = str(p).strip()
        if p and '@' in p and p.lower() not in [x.lower() for x in out]:
            out.append(p)
    return out


def _reply_subject(subject: str) -> str:
    subject = (subject or '').strip() or 'Your inquiry'
    return subject if subject.lower().startswith('re:') else f'Re: {subject}'


class EmailDispatchService:

    @classmethod
    def dispatch_receptionist_reply(cls, lead: ReceptionistLead, draft_text: str,
                                    selected_slot: Optional[str] = None, subject: Optional[str] = None,
                                    cc: Optional[list] = None) -> bool:
        if not lead.sender_email:
            logger.warning(f'EmailDispatch: lead #{lead.id} has no email address.')
            return False

        from email_receptionist.services.ai_triage_service import AITriageService
        if AITriageService.is_automated_sender(lead.sender_email):
            logger.warning(f'EmailDispatch: blocked reply to automated/no-reply address: {lead.sender_email}')
            return False

        draft_text = (draft_text or '').strip()
        if not draft_text:
            logger.warning(f'EmailDispatch: empty reply for lead #{lead.id}, not sending.')
            return False

        cfg = ReceptionistConfig.objects.filter(tenant=lead.tenant).first() if lead.tenant else None
        tenant_name = lead.tenant.name if lead.tenant else 'Our team'
        email_subject = _reply_subject(subject or lead.subject)
        # CC: what the user typed, else the thread's CC (set by a rule); never the customer themself
        cc = split_emails(lead.cc_emails if cc is None else cc)
        cc = [a for a in cc if a.lower() != (lead.sender_email or '').lower()]

        # Threading headers from the latest inbound message
        last_in = lead.messages.filter(direction='inbound').order_by('-sent_at').first()
        in_reply_to = (last_in.message_id if last_in else '') or ''
        references = ' '.join(x for x in [(last_in.references if last_in else ''), in_reply_to] if x).strip()

        from_email = ((cfg.mailbox_connected_email or cfg.gmail_connected_email) if cfg else None) \
            or getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@localhost')
        domain = from_email.split('@')[-1] if '@' in from_email else None
        our_message_id = make_msgid(domain=domain)

        html_content = cls._build_html_email(body_text=draft_text, tenant_name=tenant_name, slot=selected_slot)
        headers = {'Message-ID': our_message_id}
        if in_reply_to:
            headers['In-Reply-To'] = in_reply_to
        if references:
            headers['References'] = references

        has_creds = bool(cfg and (
            cfg.mailbox_app_password or cfg.gmail_app_password
            or cfg.google_access_token or cfg.google_refresh_token
            or cfg.microsoft_access_token or cfg.microsoft_refresh_token
        ))
        if cfg and (cfg.mailbox_connected_email or cfg.gmail_connected_email) and has_creds:
            sent_ok = cls._send_via_tenant_mailbox(
                cfg=cfg, to_email=lead.sender_email, subject=email_subject, plain_text=draft_text,
                html_text=html_content, headers=headers,
                graph_reply_to_id=(last_in.provider_message_id if last_in else ''), cc=cc,
            )
        else:
            sent_ok = cls._send_via_django_mail(lead.sender_email, email_subject, draft_text, html_content, headers, cc=cc)

        if not sent_ok:
            lead.status = 'awaiting'
            lead.scheduled_send_at = None
            lead.escalation_reason = 'Sending failed - check the mailbox connection and try again.'
            lead.save(update_fields=['status', 'scheduled_send_at', 'escalation_reason', 'updated_at'])
            return False

        lead.status = 'replied'
        lead.ai_draft_response = draft_text
        lead.selected_slot = selected_slot or None
        lead.replied_at = timezone.now()
        lead.scheduled_send_at = None
        lead.save()

        ReceptionistEmailMessage.objects.create(
            lead=lead, direction='outbound', sender=from_email,
            recipient=(f"{lead.sender_email} (cc: {', '.join(cc)})" if cc else lead.sender_email)[:255],
            subject=email_subject, body_text=draft_text, body_html=html_content, sent_at=timezone.now(),
            message_id=our_message_id, in_reply_to=in_reply_to, references=references,
            **({'organization': lead.tenant} if lead.tenant_id and hasattr(ReceptionistEmailMessage, 'organization') else {}),
        )
        return True

    @classmethod
    def forward_email(cls, cfg, lead: ReceptionistLead, to_list, note: str = '') -> str:
        """
        Forwards the latest email of a thread to colleagues (a rule's "forward to").
        Sent from the connected mailbox (platform email as a fallback); Reply-To is the customer,
        so the colleague can simply press Reply. Returns 'mailbox' | 'platform' | 'failed'.
        """
        to_list = split_emails(to_list)
        if not to_list:
            return 'failed'
        last_in = lead.messages.filter(direction='inbound').order_by('-sent_at').first()
        original = (last_in.body_text if last_in else lead.inbound_message) or ''
        subject = (last_in.subject if last_in else lead.subject) or 'Enquiry'
        when = timezone.localtime(last_in.sent_at).strftime('%d %b %Y %H:%M') if last_in and last_in.sent_at else ''
        body = (
            (f"{note}\n\n" if note else '')
            + "---------- Forwarded message ----------\n"
            f"From: {lead.sender_name} <{lead.sender_email}>\n"
            + (f"Date: {when}\n" if when else '')
            + f"Subject: {subject}\n\n{original}"
        )
        fwd_subject = subject if subject.lower().startswith('fwd:') else f'Fwd: {subject}'
        return cls.send_internal_alert(cfg, to_list, fwd_subject, body, reply_to=lead.sender_email, one_message=True)

    @classmethod
    def send_test_email(cls, cfg, to_email: str):
        """Sends one test message from the organization's connected mailbox. Returns (ok, message)."""
        if not cfg:
            return False, 'No mailbox settings found.'
        from_email = cfg.mailbox_connected_email or cfg.gmail_connected_email
        has_creds = bool(cfg.mailbox_app_password or cfg.gmail_app_password or cfg.google_access_token
                         or cfg.google_refresh_token or cfg.microsoft_access_token or cfg.microsoft_refresh_token)
        if not from_email or not has_creds:
            return False, 'Connect and save a mailbox first (email + app password, or 1-click sign-in).'
        company = cfg.tenant.name if cfg.tenant else 'your company'
        body = (
            "Hello,\n\n"
            f"This is a test email from the {company} Email Receptionist.\n"
            f"It was sent from {from_email}, which means sending works.\n\n"
            "You can ignore or delete this message."
        )
        domain = from_email.split('@')[-1] if '@' in from_email else None
        ok = cls._send_via_tenant_mailbox(
            cfg=cfg, to_email=to_email, subject=f'Test email from {company} Email Receptionist',
            plain_text=body, html_text=cls._build_html_email(body_text=body, tenant_name=company),
            headers={'Message-ID': make_msgid(domain=domain)},
        )
        if ok:
            return True, f'Test email sent from {from_email} to {to_email}. Check that inbox (and spam folder).'
        return False, (f'Could not send from {from_email}. Check the app password / sign-in and the SMTP settings, '
                       f'then try again. Details are in the server log.')

    @staticmethod
    def mailbox_ready(cfg) -> bool:
        """True when the organization has a connected mailbox we can send from."""
        if not cfg:
            return False
        has_creds = bool(cfg.mailbox_app_password or cfg.gmail_app_password or cfg.google_access_token
                         or cfg.google_refresh_token or cfg.microsoft_access_token or cfg.microsoft_refresh_token)
        return bool((cfg.mailbox_connected_email or cfg.gmail_connected_email) and has_creds)

    @classmethod
    def send_internal_alert(cls, cfg, recipients, subject: str, body: str, reply_to: str = '',
                            one_message: bool = False) -> str:
        """
        Sends a notification to the organization's own team (e.g. an escalation alert).
        Sent FROM the organization's connected mailbox when there is one; if there is no mailbox,
        or sending from it fails, falls back to the platform address (DEFAULT_FROM_EMAIL / SES)
        so the alert is never lost. Returns 'mailbox', 'platform' or 'failed'.
        """
        recipients = [r for r in recipients if r]
        if not recipients:
            return 'failed'
        tenant_name = cfg.tenant.name if cfg and cfg.tenant else 'Your team'
        html_text = cls._build_html_email(body_text=body, tenant_name=tenant_name)
        # RFC 3834: marks it as machine-generated so no auto-responder (including ours) answers it
        base_headers = {'Auto-Submitted': 'auto-generated', 'X-Auto-Response-Suppress': 'All'}
        if reply_to:
            base_headers['Reply-To'] = reply_to

        if cls.mailbox_ready(cfg):
            from_email = cfg.mailbox_connected_email or cfg.gmail_connected_email
            domain = from_email.split('@')[-1] if '@' in from_email else None
            ok_all = True
            batches = [recipients] if one_message else [[r] for r in recipients]
            for batch in batches:
                headers = dict(base_headers, **{'Message-ID': make_msgid(domain=domain)})
                if not cls._send_via_tenant_mailbox(cfg=cfg, to_email=batch[0], subject=subject, plain_text=body,
                                                    html_text=html_text, headers=headers, extra_to=batch[1:],
                                                    reply_to=reply_to):
                    ok_all = False
                    break
            if ok_all:
                return 'mailbox'
            logger.warning('EmailDispatch: alert could not be sent from the connected mailbox, using platform email.')

        try:
            from django.core.mail import EmailMultiAlternatives
            msg = EmailMultiAlternatives(subject=subject, body=body,
                                         from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                                         to=recipients, headers=base_headers)
            msg.attach_alternative(html_text, 'text/html')
            msg.send(fail_silently=False)
            return 'platform'
        except Exception as err:
            logger.warning(f'EmailDispatch: alert via platform email failed: {err}')
            return 'failed'

    # ── Senders ──────────────────────────────────────────────────────────
    @classmethod
    def _send_via_tenant_mailbox(cls, cfg, to_email, subject, plain_text, html_text, headers, graph_reply_to_id='',
                                 cc=None, extra_to=None, reply_to='') -> bool:
        import base64
        from email_receptionist.services.imap_poller_service import IMAPPollerService

        cc, extra_to = list(cc or []), list(extra_to or [])
        if cls._is_microsoft_oauth(cfg):
            return cls._send_via_microsoft_graph(cfg, to_email, subject, plain_text, html_text, graph_reply_to_id,
                                                 cc=cc, extra_to=extra_to, reply_to=reply_to)

        smtp_host = cfg.smtp_host or cls._default_smtp_host(cfg.email_provider)
        smtp_port = cfg.smtp_port or 587
        from_email = cfg.mailbox_connected_email or cfg.gmail_connected_email
        display_name = cfg.mailbox_display_name or (cfg.tenant.name if cfg.tenant else '')
        app_password = cfg.mailbox_app_password or cfg.gmail_app_password
        access_token = IMAPPollerService._get_valid_access_token(cfg) if (cfg.google_access_token or cfg.google_refresh_token) else None

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((display_name, from_email)) if display_name else from_email
        msg['To'] = ', '.join([to_email] + extra_to)
        if cc:
            msg['Cc'] = ', '.join(cc)
        for k, v in headers.items():
            msg[k] = v
        msg.attach(MIMEText(plain_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_text, 'html', 'utf-8'))

        try:
            use_ssl = cfg.use_ssl_tls and smtp_port == 465
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) if use_ssl else smtplib.SMTP(smtp_host, smtp_port, timeout=20)
            with server:
                if not use_ssl:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                if access_token:
                    xoauth2 = base64.b64encode(f"user={from_email}\x01auth=Bearer {access_token}\x01\x01".encode()).decode()
                    code, resp = server.docmd('AUTH', 'XOAUTH2 ' + xoauth2)
                    if code != 235:
                        raise smtplib.SMTPAuthenticationError(code, resp)
                else:
                    server.login(from_email, app_password)
                server.sendmail(from_email, [to_email] + extra_to + cc, msg.as_string())
            logger.info(f'EmailDispatch: sent via tenant SMTP ({from_email}) to {to_email}')
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error(f'EmailDispatch: SMTP authentication failed for {from_email}.')
        except Exception as err:
            logger.warning(f'EmailDispatch: SMTP error ({smtp_host}:{smtp_port}): {err}')
        # Do NOT silently fall back to the platform address: the customer would get a reply
        # from an unexpected sender. Report the failure so a human can fix the mailbox.
        return False

    @staticmethod
    def _is_microsoft_oauth(cfg) -> bool:
        return bool(cfg.microsoft_access_token or cfg.microsoft_refresh_token) and cfg.email_provider == 'outlook'

    @classmethod
    def _send_via_django_mail(cls, to_email, subject, plain_text, html_text, headers, cc=None) -> bool:
        try:
            from django.core.mail import EmailMultiAlternatives
            msg = EmailMultiAlternatives(
                subject=subject, body=plain_text,
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                to=[to_email], cc=list(cc or []), headers=headers,
            )
            msg.attach_alternative(html_text, 'text/html')
            msg.send(fail_silently=False)
            logger.info(f'EmailDispatch: sent via Django mail backend to {to_email}')
            return True
        except Exception as err:
            logger.warning(f'EmailDispatch: Django mail send failed: {err}')
            return False

    @classmethod
    def _send_via_microsoft_graph(cls, cfg, to_email, subject, plain_text, html_text, reply_to_id='',
                                  cc=None, extra_to=None, reply_to='') -> bool:
        import requests
        from email_receptionist.services.imap_poller_service import IMAPPollerService

        token = IMAPPollerService._get_valid_access_token(cfg)
        if not token:
            logger.warning('EmailDispatch: no valid Microsoft OAuth token.')
            return False
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        body = {'contentType': 'HTML', 'content': html_text or html.escape(plain_text)}
        addr = lambda a: {'emailAddress': {'address': a}}  # noqa: E731
        cc_list = [addr(a) for a in (cc or [])]

        # Reply inside the original Outlook conversation when we know the message id
        if reply_to_id:
            try:
                r = requests.post(f'https://graph.microsoft.com/v1.0/me/messages/{reply_to_id}/reply',
                                  headers=headers, json={'message': {'body': body, **({'ccRecipients': cc_list} if cc_list else {})}},
                                  timeout=15)
                if r.status_code in (200, 202):
                    return True
                logger.warning(f'EmailDispatch: Graph in-thread reply failed ({r.status_code}), falling back to sendMail.')
            except Exception as e:
                logger.warning(f'EmailDispatch: Graph reply error: {e}')

        payload = {
            'message': {'subject': subject, 'body': body,
                        'toRecipients': [addr(a) for a in [to_email] + list(extra_to or [])],
                        **({'ccRecipients': cc_list} if cc_list else {}),
                        **({'replyTo': [addr(reply_to)]} if reply_to else {})},
            'saveToSentItems': 'true',
        }
        try:
            r = requests.post('https://graph.microsoft.com/v1.0/me/sendMail', headers=headers, json=payload, timeout=15)
            if r.status_code in (200, 202):
                return True
            logger.error(f'EmailDispatch: Graph sendMail failed ({r.status_code}): {r.text[:500]}')
        except Exception as e:
            logger.error(f'EmailDispatch: Graph sendMail error: {e}')
        return False

    @staticmethod
    def _default_smtp_host(provider: str) -> str:
        return {'gmail': 'smtp.gmail.com', 'outlook': 'smtp.office365.com', 'zoho': 'smtp.zoho.com'}.get(provider, 'smtp.gmail.com')

    @staticmethod
    def _build_html_email(body_text: str, tenant_name: str, slot: Optional[str] = None, **_ignored) -> str:
        """Simple, clean HTML version of the reply. All user/AI text is HTML-escaped."""
        body = html.escape(body_text).replace('\n', '<br>')
        slot_html = ''
        if slot:
            slot_html = (
                '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;margin:18px 0;">'
                '<div style="font-size:11px;font-weight:700;color:#475569;text-transform:uppercase;margin-bottom:4px;">Suggested time</div>'
                f'<div style="font-size:14px;font-weight:600;color:#0f172a;">{html.escape(slot)}</div></div>'
            )
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
            '<body style="margin:0;padding:0;font-family:Segoe UI,Arial,sans-serif;">'
            f'<div style="font-size:14px;line-height:1.6;color:#1e293b;max-width:640px;">{body}{slot_html}</div>'
            '</body></html>'
        )
