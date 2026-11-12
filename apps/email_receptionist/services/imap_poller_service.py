"""
IMAPPollerService — On-Demand Mailbox Polling
==============================================
Uses Python's built-in imaplib (zero cost, no external API).

Connects to the tenant's configured IMAP server, fetches the last days' emails it has not seen yet
(by Message-ID, so mail already opened in the inbox is not missed),
ingests them through IngestionService, and marks each as read.

Designed to be called:
  - Via the "Check Inbox" button in the dashboard (api_poll_mailbox view)
  - Via `python manage.py poll_mailbox` cron/management command
"""

import imaplib
import email
import logging
from email.header import decode_header
from datetime import timedelta
from typing import Optional
from django.utils import timezone

logger = logging.getLogger(__name__)

RECENT_DAYS = 2      # read mail that arrived in this window (already-ingested Message-IDs are skipped)
MAX_PER_POLL = 50


class IMAPPollerService:

    @classmethod
    def poll(cls, tenant, config) -> dict:
        """Read the mailbox and remember when, and why it failed, for the settings screen."""
        result = cls._poll(tenant, config)
        if config is not None and getattr(config, 'pk', None):
            try:
                type(config).objects.filter(pk=config.pk).update(
                    mailbox_last_polled_at=timezone.now(),
                    mailbox_last_error='' if not result.get('errors') or result.get('ingested') else '; '.join(map(str, result['errors']))[:1000])
            except Exception:
                logger.exception('IMAPPoller: could not record the poll status')
        return result

    @classmethod
    def _poll(cls, tenant, config) -> dict:
        """
        Main entry point. Polls the tenant's connected mailbox for unseen emails.

        Returns a summary dict:
          { 'ingested': int, 'skipped_spam': int, 'errors': list[str], 'last_polled': str }
        """
        result = {
            'ingested': 0,
            'skipped_spam': 0,
            'errors': [],
            'last_polled': timezone.now().strftime('%I:%M %p'),
        }

        if not config:
            result['errors'].append('No receptionist configuration found.')
            return result

        email_address = config.mailbox_connected_email or config.gmail_connected_email
        app_password = config.mailbox_app_password or config.gmail_app_password
        access_token = cls._get_valid_access_token(config)

        if not email_address or (not app_password and not access_token):
            if (config.email_provider or '') in ('zoho', 'smtp_imap') and email_address:
                result['errors'].append('Reading this mailbox needs an app password: add it in Email settings '
                                        '(Zoho Mail → Settings → Security → App Passwords).')
            else:
                result['errors'].append('No mailbox connected. Sign in or add an app password in Email settings.')
            return result

        imap_host = config.imap_host or cls._default_imap_host(config.email_provider)
        imap_port = config.imap_port or 993

        # If connected via Microsoft OAuth, poll directly via official Microsoft Graph API
        # (Microsoft disables IMAP basic auth and legacy IMAP XOAUTH2 on consumer accounts)
        # the provider picked in settings decides: a login left over from an earlier provider must not win
        is_microsoft_oauth = (
            config.email_provider == 'outlook'
            and (getattr(config, 'microsoft_access_token', None) or getattr(config, 'microsoft_refresh_token', None))
        )
        if is_microsoft_oauth:
            return cls._poll_microsoft_graph(tenant, config)

        try:
            # Connect to IMAP
            if config.use_ssl_tls:
                mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            else:
                mail = imaplib.IMAP4(imap_host, imap_port)

            if access_token:
                xoauth2_str = f"user={email_address}\x01auth=Bearer {access_token}\x01\x01"
                mail.authenticate('XOAUTH2', lambda x: xoauth2_str)
            else:
                mail.login(email_address, app_password)

            mail.select('INBOX')

            # Everything that arrived in the last 2 days, not only unread mail: an email someone already opened in
            # Gmail/Outlook must still reach the receptionist. Emails we already have (same Message-ID) are skipped
            # after reading only their header, so this stays cheap.
            since = (timezone.now() - timedelta(days=RECENT_DAYS)).strftime('%d-%b-%Y')
            status, messages = mail.search(None, f'(SINCE {since})')
            if status != 'OK':
                result['errors'].append('Failed to search inbox.')
                mail.logout()
                return result

            email_ids = [x for x in messages[0].split() if x][-MAX_PER_POLL:]
            email_ids = cls._new_ids(mail, email_ids, tenant)
            if not email_ids:
                mail.logout()
                return result  # No new emails

            from email_receptionist.services.ingestion_service import IngestionService

            for eid in email_ids:
                try:
                    res, msg_data = mail.fetch(eid, '(BODY.PEEK[])')  # PEEK: don't mark read until ingested
                    if res != 'OK' or not msg_data or not msg_data[0]:
                        continue

                    raw_email = msg_data[0][1]
                    parsed = email.message_from_bytes(raw_email)

                    subject = cls._decode_header_value(parsed.get('Subject', 'Inbound Inquiry'))
                    from_field = cls._decode_header_value(parsed.get('From', ''))
                    sender_name, sender_email = cls._parse_from_field(from_field)
                    body = cls._extract_body(parsed)

                    # Skip emails from self (loop prevention)
                    if sender_email and email_address and sender_email.lower() == email_address.lower():
                        # Mark as read and skip
                        mail.store(eid, '+FLAGS', '\\Seen')
                        continue

                    lead, created, is_spam = IngestionService.ingest_lead_or_message(
                        tenant=tenant,
                        sender_name=sender_name or 'Unknown Sender',
                        sender_email=sender_email or '',
                        subject=subject,
                        message=body,
                        source='IMAP Mailbox',
                        message_id=(parsed.get('Message-ID') or '').strip(),
                        in_reply_to=(parsed.get('In-Reply-To') or '').strip(),
                        references=' '.join((parsed.get('References') or '').split()),
                        is_bulk=cls.is_bulk_or_auto(parsed),
                        recipient=email_address,
                    )

                    # Mark as read after successful ingestion
                    mail.store(eid, '+FLAGS', '\\Seen')

                    if is_spam:
                        result['skipped_spam'] += 1
                    else:
                        result['ingested'] += 1

                except Exception as msg_err:
                    logger.warning(f'IMAPPoller: Error processing email {eid}: {msg_err}')
                    result['errors'].append(str(msg_err))

            try:
                mail.logout()
            except Exception:
                pass

        except imaplib.IMAP4.error as imap_err:
            error_msg = str(imap_err)
            logger.error(f'IMAPPoller IMAP error: {error_msg}')
            if 'AUTHENTICATIONFAILED' in error_msg.upper() or 'Invalid credentials' in error_msg:
                result['errors'].append(
                    'Authentication failed. Check your email and App Password in Mailbox Config. '
                    'For Gmail: generate an App Password at myaccount.google.com/apppasswords.'
                )
            else:
                result['errors'].append(f'IMAP connection error: {error_msg}')
        except ConnectionRefusedError:
            result['errors'].append(f'Could not connect to {imap_host}:{imap_port}. Check host/port settings.')
        except Exception as err:
            logger.error(f'IMAPPoller unexpected error: {err}')
            result['errors'].append(str(err))

        return result

    @staticmethod
    def _new_ids(mail, email_ids, tenant):
        """The ids among email_ids whose Message-ID we have not ingested yet (unread mail without one is kept)."""
        from email_receptionist.models import ReceptionistEmailMessage
        if not email_ids:
            return []
        keep, by_mid = [], {}
        for eid in email_ids:
            try:
                res, data = mail.fetch(eid, '(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            except Exception:
                keep.append(eid)
                continue
            if res != 'OK' or not data or not data[0]:
                continue
            flags = data[0][0] if isinstance(data[0], tuple) else b''
            header = data[0][1] if isinstance(data[0], tuple) else b''
            mid = email.message_from_bytes(header or b'').get('Message-ID', '').strip()
            if mid:
                by_mid[mid] = eid
            elif b'\\Seen' not in (flags or b''):
                keep.append(eid)
        if by_mid:
            have = set(ReceptionistEmailMessage.objects.filter(lead__tenant=tenant, direction='inbound',
                                                               message_id__in=list(by_mid)).values_list('message_id', flat=True))
            keep += [eid for mid, eid in by_mid.items() if mid not in have]
        return sorted(keep, key=lambda x: int(x))

    @classmethod
    def test_connection(cls, config) -> dict:
        """
        Lightweight connection test — called by the "Test Connection" button.
        Attempts IMAP login and returns status without fetching emails.
        """
        email_address = config.mailbox_connected_email or getattr(config, 'gmail_connected_email', None)
        app_password = config.mailbox_app_password or getattr(config, 'gmail_app_password', None)
        access_token = cls._get_valid_access_token(config) if (getattr(config, 'google_access_token', None) or getattr(config, 'google_refresh_token', None)) else None

        if not email_address or (not app_password and not access_token):
            return {
                'status': 'error',
                'message': 'No credentials saved. Enter your email and App Password or connect via 1-Click OAuth.',
            }

        # For Microsoft OAuth, test connection directly via Graph API
        is_microsoft_oauth = (
            (config.email_provider == 'outlook' or getattr(config, 'microsoft_access_token', None))
            and (getattr(config, 'microsoft_access_token', None) or getattr(config, 'microsoft_refresh_token', None))
        )
        if is_microsoft_oauth:
            token = cls._get_valid_access_token(config)
            if not token:
                return {'status': 'error', 'message': 'Microsoft OAuth token expired or invalid. Please re-authenticate.'}
            import requests
            try:
                r = requests.get(
                    'https://graph.microsoft.com/v1.0/me/mailFolders/inbox',
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=8
                )
                if r.status_code == 200:
                    data = r.json()
                    unread = data.get('unreadItemCount', 0)
                    return {
                        'status': 'success',
                        'message': f'Connected to Outlook via Microsoft Graph API ({email_address})',
                        'unread_count': unread,
                        'provider': 'outlook'
                    }
                else:
                    return {'status': 'error', 'message': f'Microsoft Graph connection error: {r.text}'}
            except Exception as e:
                return {'status': 'error', 'message': f'Microsoft Graph error: {e}'}

        imap_host = config.imap_host or cls._default_imap_host(config.email_provider)
        imap_port = config.imap_port or 993

        try:
            if config.use_ssl_tls:
                mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            else:
                mail = imaplib.IMAP4(imap_host, imap_port)

            if access_token:
                xoauth2_str = f"user={email_address}\x01auth=Bearer {access_token}\x01\x01"
                mail.authenticate('XOAUTH2', lambda x: xoauth2_str)
            else:
                mail.login(email_address, app_password)

            mail.select('INBOX')
            status, messages = mail.search(None, 'UNSEEN')
            unread_count = len(messages[0].split()) if status == 'OK' and messages[0] else 0
            mail.logout()

            return {
                'status': 'success',
                'message': f'Connected to {imap_host} as {email_address}',
                'unread_count': unread_count,
                'provider': config.email_provider,
            }

        except imaplib.IMAP4.error as imap_err:
            error_str = str(imap_err)
            if 'AUTHENTICATIONFAILED' in error_str.upper() or 'Invalid credentials' in error_str:
                hint = ''
                if config.email_provider == 'gmail':
                    hint = ' For Gmail: use a 16-char App Password (not your regular password).'
                elif config.email_provider == 'outlook':
                    hint = ' For Outlook: enable IMAP in account settings and use an App Password.'
                return {'status': 'auth_error', 'message': f'Authentication failed.{hint}'}
            return {'status': 'error', 'message': f'IMAP error: {error_str}'}
        except Exception as err:
            return {'status': 'error', 'message': str(err)}

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def is_bulk_or_auto(headers) -> bool:
        """
        True for auto-replies, bounces, newsletters and mailing lists (RFC 3834 / common headers).
        Never auto-reply to these - it causes mail loops and looks like spam.
        `headers` can be an email.message.Message or a dict with lower-case keys.
        """
        def get(name):
            try:
                return (headers.get(name) or headers.get(name.lower()) or '').strip().lower()
            except Exception:
                return ''
        auto_submitted = get('Auto-Submitted')
        if auto_submitted and auto_submitted != 'no':
            return True
        if get('Precedence') in ('bulk', 'list', 'junk', 'auto_reply'):
            return True
        if get('List-Unsubscribe') or get('List-Id'):
            return True
        if get('X-Autoreply') or get('X-Autorespond') or get('X-Auto-Response-Suppress') in ('all', 'oof', 'autoreply'):
            return True
        return False

    @staticmethod
    def acquire_lock(cfg, seconds: int = 300) -> bool:
        """DB-level lock (works across web + Celery processes, no shared cache needed)."""
        from django.db.models import Q
        from email_receptionist.models import ReceptionistConfig
        now = timezone.now()
        return bool(ReceptionistConfig.objects.filter(pk=cfg.pk).filter(
            Q(poll_lock_until__isnull=True) | Q(poll_lock_until__lt=now)
        ).update(poll_lock_until=now + timezone.timedelta(seconds=seconds)))

    @staticmethod
    def release_lock(cfg) -> None:
        from email_receptionist.models import ReceptionistConfig
        ReceptionistConfig.objects.filter(pk=cfg.pk).update(poll_lock_until=None)

    @classmethod
    def poll_all(cls, tenant_pk=None) -> dict:
        """Polls every organization with mailbox sync enabled. Used by Celery beat and the management command."""
        from django.db.models import Q
        from email_receptionist.models import ReceptionistConfig

        qs = ReceptionistConfig.objects.filter(mailbox_sync_enabled=True).filter(
            Q(mailbox_connected_email__isnull=False) | Q(gmail_connected_email__isnull=False)
        ).filter(
            Q(mailbox_app_password__isnull=False) | Q(gmail_app_password__isnull=False) |
            Q(google_access_token__isnull=False) | Q(google_refresh_token__isnull=False) |
            Q(microsoft_access_token__isnull=False) | Q(microsoft_refresh_token__isnull=False)
        ).select_related('tenant')
        if tenant_pk:
            qs = qs.filter(tenant__pk=tenant_pk)

        totals = {'mailboxes': 0, 'ingested': 0, 'skipped_spam': 0, 'errors': []}
        for cfg in qs:
            if not cls.acquire_lock(cfg):
                continue  # another worker is already polling this mailbox
            try:
                res = cls.poll(tenant=cfg.tenant, config=cfg)
                totals['mailboxes'] += 1
                totals['ingested'] += res['ingested']
                totals['skipped_spam'] += res['skipped_spam']
                totals['errors'] += [f'{cfg.mailbox_connected_email}: {e}' for e in res['errors']]
            except Exception as e:
                logger.error(f'IMAPPoller: polling failed for config #{cfg.pk}: {e}', exc_info=True)
                totals['errors'].append(str(e))
            finally:
                cls.release_lock(cfg)
        return totals

    @staticmethod
    def _default_imap_host(provider: str) -> str:
        return {
            'gmail': 'imap.gmail.com',
            'outlook': 'outlook.office365.com',
            'zoho': 'imap.zoho.com',
        }.get(provider, 'imap.gmail.com')

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """Decodes RFC2047 encoded email headers."""
        try:
            parts = decode_header(value)
            decoded = ''
            for part, charset in parts:
                if isinstance(part, bytes):
                    decoded += part.decode(charset or 'utf-8', errors='replace')
                else:
                    decoded += part
            return decoded.strip()
        except Exception:
            return value or ''

    @staticmethod
    def _parse_from_field(from_field: str):
        """Parses 'Name <email@domain.com>' or 'email@domain.com' format."""
        try:
            if '<' in from_field and '>' in from_field:
                name_part = from_field[:from_field.index('<')].strip().strip('"')
                email_part = from_field[from_field.index('<') + 1:from_field.index('>')].strip()
                return name_part or email_part.split('@')[0], email_part
            else:
                addr = from_field.strip()
                return addr.split('@')[0] if '@' in addr else addr, addr
        except Exception:
            return 'Unknown Sender', ''

    @classmethod
    def _extract_body(cls, msg) -> str:
        """Extracts and cleans body text from a parsed email message."""
        body = ''
        html_body = ''
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    disposition = str(part.get('Content-Disposition', ''))
                    if 'attachment' in disposition:
                        continue
                    if content_type == 'text/plain' and not body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or 'utf-8'
                            body = payload.decode(charset, errors='replace')
                    elif content_type == 'text/html' and not html_body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or 'utf-8'
                            html_body = payload.decode(charset, errors='replace')
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or 'utf-8'
                    if msg.get_content_type() == 'text/html':
                        html_body = payload.decode(charset, errors='replace')
                    else:
                        body = payload.decode(charset, errors='replace')
        except Exception as e:
            logger.warning(f'IMAPPoller: Could not extract body: {e}')

        final_text = body or cls._strip_html_tags(html_body)
        return cls._clean_body_text(final_text)

    @staticmethod
    def _strip_html_tags(html_str: str) -> str:
        if not html_str:
            return ""
        import re
        # Remove script and style elements
        html_str = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', html_str, flags=re.IGNORECASE)
        html_str = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html_str, flags=re.IGNORECASE)
        # Replace <br> and <p> with newlines
        html_str = re.sub(r'<br\s*/?>', '\n', html_str, flags=re.IGNORECASE)
        html_str = re.sub(r'</p>', '\n', html_str, flags=re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r'<[^>]+>', ' ', html_str)
        return text

    @staticmethod
    def _clean_body_text(text: str) -> str:
        if not text:
            return ""
        import re, html
        text = html.unescape(text)

        # Collapse linebreaks that occur within angle-bracketed URLs e.g. <https://... \r\n hash>
        text = re.sub(r'<https?://([^>]+)>', lambda m: '<' + re.sub(r'\s+', '', m.group(0)[1:-1]) + '>', text)

        # Normalize multiple spaces and blank lines
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n\s*\n', '\n\n', text)
        return text.strip()[:4000]

    @classmethod
    def _get_valid_access_token(cls, config) -> Optional[str]:
        """Refreshes and returns valid OAuth access token if available (Google or Microsoft)."""
        provider = (getattr(config, 'email_provider', '') or 'gmail').lower()
        if provider not in ('gmail', 'outlook'):
            return None  # Zoho / custom IMAP read with the app password

        if provider == 'outlook':
            if not getattr(config, 'microsoft_refresh_token', None):
                return getattr(config, 'microsoft_access_token', None)

            if config.microsoft_access_token and getattr(config, 'microsoft_token_expiry', None) and config.microsoft_token_expiry > timezone.now() + timezone.timedelta(seconds=60):
                return config.microsoft_access_token

            import requests, os
            from django.conf import settings
            client_id = getattr(settings, 'MICROSOFT_OAUTH_CLIENT_ID', None) or os.environ.get('MICROSOFT_OAUTH_CLIENT_ID')
            client_secret = getattr(settings, 'MICROSOFT_OAUTH_CLIENT_SECRET', None) or os.environ.get('MICROSOFT_OAUTH_CLIENT_SECRET')

            if not client_id or not client_secret:
                return getattr(config, 'microsoft_access_token', None)

            try:
                res = requests.post('https://login.microsoftonline.com/common/oauth2/v2.0/token', data={
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'refresh_token': config.microsoft_refresh_token,
                    'grant_type': 'refresh_token'
                }, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    config.microsoft_access_token = data.get('access_token')
                    expires_in = data.get('expires_in', 3600)
                    config.microsoft_token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)
                    config.save(update_fields=['microsoft_access_token', 'microsoft_token_expiry'])
                    return config.microsoft_access_token
            except Exception as e:
                logger.warning(f"Failed to refresh Microsoft OAuth token: {e}")

            return getattr(config, 'microsoft_access_token', None)

        # Default to Google OAuth token resolution
        if not getattr(config, 'google_refresh_token', None):
            return getattr(config, 'google_access_token', None)

        if config.google_access_token and getattr(config, 'google_token_expiry', None) and config.google_token_expiry > timezone.now() + timezone.timedelta(seconds=60):
            return config.google_access_token

        import requests, os
        from django.conf import settings
        client_id = getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', None) or os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
        client_secret = getattr(settings, 'GOOGLE_OAUTH_CLIENT_SECRET', None) or os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')

        if not client_id or not client_secret:
            return getattr(config, 'google_access_token', None)

        try:
            res = requests.post('https://oauth2.googleapis.com/token', data={
                'client_id': client_id,
                'client_secret': client_secret,
                'refresh_token': config.google_refresh_token,
                'grant_type': 'refresh_token'
            }, timeout=10)
            if res.status_code == 200:
                data = res.json()
                config.google_access_token = data.get('access_token')
                expires_in = data.get('expires_in', 3600)
                config.google_token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in)
                config.save(update_fields=['google_access_token', 'google_token_expiry'])
                return config.google_access_token
        except Exception as e:
            logger.warning(f"Failed to refresh Google OAuth token: {e}")

        return getattr(config, 'google_access_token', None)

    @classmethod
    def _poll_microsoft_graph(cls, tenant, config) -> dict:
        """
        Polls unread emails via Microsoft Graph REST API:
        GET /v1.0/me/mailFolders/inbox/messages?$filter=isRead eq false
        Marks them as read via PATCH after ingestion.
        """
        import requests
        from email_receptionist.services.ingestion_service import IngestionService

        result = {
            'ingested': 0,
            'skipped_spam': 0,
            'errors': [],
            'last_polled': timezone.now().strftime('%I:%M %p'),
        }

        access_token = cls._get_valid_access_token(config)
        if not access_token:
            result['errors'].append('Microsoft account not authorized or token expired.')
            return result

        email_address = config.mailbox_connected_email or config.gmail_connected_email or ''
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'Prefer': 'outlook.body-content-type="text"'
        }

        since = (timezone.now() - timedelta(days=RECENT_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        url = ("https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
               f"?$filter=receivedDateTime ge {since}&$orderby=receivedDateTime desc&$top={MAX_PER_POLL}"
               "&$select=id,subject,body,from,receivedDateTime,internetMessageId,internetMessageHeaders")

        try:
            res = requests.get(url, headers=headers, timeout=12)
            if res.status_code != 200:
                result['errors'].append(f"Microsoft Graph API error: {res.text}")
                logger.error(f"Microsoft Graph poll failed ({res.status_code}): {res.text}")
                return result

            messages = res.json().get('value', [])
            from email_receptionist.models import ReceptionistEmailMessage
            mids = [m.get('internetMessageId') for m in messages if m.get('internetMessageId')]
            have = set(ReceptionistEmailMessage.objects.filter(lead__tenant=tenant, direction='inbound', message_id__in=mids)
                       .values_list('message_id', flat=True)) if mids else set()
            messages = [m for m in reversed(messages) if m.get('internetMessageId') not in have]  # oldest first
            for m in messages:
                msg_id = m.get('id')
                subject = m.get('subject') or 'Inbound Inquiry'
                sender_info = (m.get('from') or {}).get('emailAddress') or {}
                sender_name = sender_info.get('name') or 'Outlook Contact'
                sender_email = sender_info.get('address') or ''
                body_content = (m.get('body') or {}).get('content') or ''

                # Skip self emails
                if sender_email and sender_email.lower() == email_address.lower():
                    # Mark read
                    requests.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}", headers=headers, json={"isRead": True}, timeout=5)
                    continue

                hdrs = {h.get('name', '').lower(): h.get('value', '') for h in (m.get('internetMessageHeaders') or [])}
                lead, created, is_spam = IngestionService.ingest_lead_or_message(
                    tenant=tenant,
                    sender_name=sender_name,
                    sender_email=sender_email,
                    subject=subject,
                    message=cls._clean_body_text(body_content),
                    source='Outlook Mailbox',
                    message_id=m.get('internetMessageId') or hdrs.get('message-id', ''),
                    in_reply_to=hdrs.get('in-reply-to', ''),
                    references=' '.join(hdrs.get('references', '').split()),
                    provider_message_id=msg_id or '',
                    is_bulk=cls.is_bulk_or_auto(hdrs),
                    recipient=email_address,
                )

                if is_spam:
                    result['skipped_spam'] += 1
                else:
                    result['ingested'] += 1

                # Mark as read in Microsoft mailbox
                requests.patch(
                    f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                    headers=headers,
                    json={"isRead": True},
                    timeout=5
                )

        except Exception as e:
            logger.error(f"Error polling Microsoft Graph API: {e}")
            result['errors'].append(str(e))

        return result
