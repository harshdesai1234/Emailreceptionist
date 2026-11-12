"""
Email Receptionist tests.

Run:  python manage.py test email_receptionist
External calls (LLM, SMTP, IMAP) are mocked; nothing leaves the machine.
"""
import json
from datetime import timedelta, time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Organization
from email_receptionist.models import ReceptionistConfig, ReceptionistLead, ReceptionistEmailMessage
from email_receptionist.services.ai_draft_service import AIDraftService, _parse_llm_json
from email_receptionist.services.auto_reply_service import AutoReplyService
from email_receptionist.services.imap_poller_service import IMAPPollerService
from email_receptionist.services.ingestion_service import IngestionService

User = get_user_model()

REPLY = {'action': 'reply', 'reply': 'Hi Sam,\n\nOur retainers start at $3,500/mo.\n\nBest,\nAcme Team', 'reason': '', 'source': 'anthropic'}
REVIEW = {'action': 'review', 'reply': 'Draft', 'reason': 'not sure', 'source': 'anthropic'}
ESCALATE = {'action': 'escalate', 'reply': 'We will get back to you.', 'reason': 'Refund request', 'source': 'anthropic'}


def org_for(request):
    return Organization.objects.filter(owner=request.user).first() if request.user.is_authenticated else None


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', DEFAULT_FROM_EMAIL='noreply@platform.test')
class Base(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user('alice', 'alice@acme.test', 'pw')
        self.u2 = User.objects.create_user('bob', 'bob@other.test', 'pw')
        self.org1 = Organization.objects.create(name='Acme', owner=self.u1)
        self.org2 = Organization.objects.create(name='OtherCo', owner=self.u2)
        self.cfg1 = ReceptionistConfig.objects.create(tenant=self.org1, is_autonomous_mode=False)
        self.cfg2 = ReceptionistConfig.objects.create(tenant=self.org2, is_autonomous_mode=False)
        p = mock.patch('email_receptionist.views._org', side_effect=org_for)
        p.start()
        self.addCleanup(p.stop)
        self.gen = mock.patch.object(AIDraftService, 'generate_reply', return_value=dict(REVIEW))
        self.gen_mock = self.gen.start()
        self.addCleanup(self.gen.stop)

    def ingest(self, org, **kw):
        base = dict(tenant=org, sender_name='Sam Lee', sender_email='sam@client.io',
                    subject='Pricing question', message='How much do retainers cost?')
        base.update(kw)
        return IngestionService.ingest_lead_or_message(**base)

    def post(self, name, data):
        return self.client.post(reverse(f'email_receptionist:{name}'), data=json.dumps(data), content_type='application/json')


class OrgIsolationTests(Base):
    def test_other_org_cannot_touch_lead(self):
        lead, _, _ = self.ingest(self.org1)
        self.client.force_login(self.u2)
        for name, payload in [('api_send_reply', {'lead_id': lead.id, 'draft': 'x'}),
                              ('api_generate_draft', {'lead_id': lead.id}),
                              ('api_escalate_lead', {'lead_id': lead.id}),
                              ('api_archive_lead', {'lead_id': lead.id}),
                              ('api_cancel_auto_send', {'lead_id': lead.id})]:
            self.assertEqual(self.post(name, payload).status_code, 404, name)
        r = self.client.get(reverse('email_receptionist:api_get_lead_messages') + f'?lead_id={lead.id}')
        self.assertEqual(r.status_code, 404)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')

    def test_dashboard_shows_only_own_leads_and_no_demo_data(self):
        self.ingest(self.org1, sender_email='a@one.io', subject='Org1 secret subject')
        self.client.force_login(self.u2)
        r = self.client.get(reverse('email_receptionist:dashboard'))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn('Org1 secret subject', html)
        self.assertNotIn('Sarah Jenkins', html)
        self.assertNotIn('89.4%', html)
        self.assertEqual(ReceptionistLead.objects.filter(tenant=self.org2).count(), 0)

    def test_dashboard_escapes_script_in_email(self):
        self.ingest(self.org1, subject='</script><script>alert(1)</script>')
        self.client.force_login(self.u1)
        html = self.client.get(reverse('email_receptionist:dashboard')).content.decode()
        self.assertNotIn('</script><script>alert(1)', html)


class WebhookTests(Base):
    def test_requires_token(self):
        r = self.client.post(reverse('email_receptionist:api_inbound_webhook'), data='{}', content_type='application/json')
        self.assertEqual(r.status_code, 401)
        r = self.client.post(reverse('email_receptionist:api_inbound_webhook'), data='{"email":"x@y.io"}',
                             content_type='application/json', HTTP_X_WEBHOOK_TOKEN='wrong')
        self.assertEqual(r.status_code, 401)

    def test_valid_token_creates_lead_for_that_org(self):
        r = self.client.post(reverse('email_receptionist:api_inbound_webhook'),
                             data=json.dumps({'name': 'Web Visitor', 'email': 'v@site.io', 'message': 'Hello'}),
                             content_type='application/json', HTTP_X_WEBHOOK_TOKEN=self.cfg2.webhook_token)
        self.assertEqual(r.status_code, 200, r.content)
        lead = ReceptionistLead.objects.get(id=r.json()['lead_id'])
        self.assertEqual(lead.tenant, self.org2)

    def test_tokens_are_unique_per_org(self):
        self.assertTrue(self.cfg1.webhook_token)
        self.assertNotEqual(self.cfg1.webhook_token, self.cfg2.webhook_token)


class EncryptionTests(Base):
    def test_credentials_encrypted_at_rest(self):
        self.cfg1.mailbox_app_password = 'super-secret-pass'
        self.cfg1.google_refresh_token = 'refresh-123'
        self.cfg1.save()
        with connection.cursor() as c:
            c.execute(f'SELECT mailbox_app_password, google_refresh_token FROM {ReceptionistConfig._meta.db_table} WHERE id=%s', [self.cfg1.id])
            raw_pw, raw_tok = c.fetchone()
        self.assertTrue(raw_pw.startswith('enc::'))
        self.assertNotIn('super-secret-pass', raw_pw)
        self.assertNotIn('refresh-123', raw_tok)
        cfg = ReceptionistConfig.objects.get(id=self.cfg1.id)
        self.assertEqual(cfg.mailbox_app_password, 'super-secret-pass')
        self.assertEqual(cfg.google_refresh_token, 'refresh-123')

    def test_legacy_plaintext_still_readable(self):
        with connection.cursor() as c:
            c.execute(f'UPDATE {ReceptionistConfig._meta.db_table} SET mailbox_app_password=%s WHERE id=%s', ['plain-old', self.cfg1.id])
        self.assertEqual(ReceptionistConfig.objects.get(id=self.cfg1.id).mailbox_app_password, 'plain-old')


class ThreadingTests(Base):
    def test_reply_joins_thread_by_headers(self):
        lead, created, _ = self.ingest(self.org1, message_id='<m1@client.io>')
        self.assertTrue(created)
        lead2, created2, _ = self.ingest(self.org1, sender_email='colleague@client.io', message='Adding my colleague',
                                         message_id='<m2@client.io>', in_reply_to='<m1@client.io>')
        self.assertFalse(created2)
        self.assertEqual(lead.id, lead2.id)
        self.assertEqual(lead.messages.count(), 2)

    def test_same_message_not_processed_twice(self):
        self.ingest(self.org1, message_id='<dup@client.io>')
        self.ingest(self.org1, message_id='<dup@client.io>')
        self.assertEqual(ReceptionistEmailMessage.objects.filter(message_id='<dup@client.io>').count(), 1)
        self.assertEqual(self.gen_mock.call_count, 1)

    def test_old_thread_not_reused(self):
        lead, _, _ = self.ingest(self.org1)
        ReceptionistLead.objects.filter(id=lead.id).update(updated_at=timezone.now() - timedelta(days=45))
        lead2, created, _ = self.ingest(self.org1, message='New topic')
        self.assertTrue(created)
        self.assertNotEqual(lead.id, lead2.id)

    def test_history_passed_to_ai(self):
        lead, _, _ = self.ingest(self.org1, message_id='<a@c.io>', message='First question')
        self.ingest(self.org1, message_id='<b@c.io>', in_reply_to='<a@c.io>', message='Follow-up question')
        history = self.gen_mock.call_args.kwargs['history']
        self.assertEqual([h['body'] for h in history], ['First question', 'Follow-up question'])

    def test_outbound_reply_has_threading_headers(self):
        lead, _, _ = self.ingest(self.org1, message_id='<orig@client.io>', references='<root@client.io>')
        self.client.force_login(self.u1)
        r = self.post('api_send_reply', {'lead_id': lead.id, 'draft': 'Thanks!'})
        self.assertEqual(r.status_code, 200, r.content)
        sent = mail.outbox[-1]
        self.assertEqual(sent.extra_headers['In-Reply-To'], '<orig@client.io>')
        self.assertIn('<root@client.io>', sent.extra_headers['References'])
        self.assertIn('<orig@client.io>', sent.extra_headers['References'])
        self.assertTrue(sent.subject.startswith('Re: '))
        out = lead.messages.get(direction='outbound')
        self.assertTrue(out.message_id)
        # customer answers our reply -> same thread
        lead2, created, _ = self.ingest(self.org1, message_id='<ans@client.io>', in_reply_to=out.message_id)
        self.assertFalse(created)
        self.assertEqual(lead2.id, lead.id)


class SpamTests(Base):
    def test_bulk_headers_never_answered(self):
        self.cfg1.is_autonomous_mode = True
        self.cfg1.save()
        lead, _, is_spam = self.ingest(self.org1, is_bulk=True)
        self.assertTrue(is_spam)
        self.assertEqual(lead.status, 'archived')
        self.gen_mock.assert_not_called()

    def test_header_detection(self):
        self.assertTrue(IMAPPollerService.is_bulk_or_auto({'auto-submitted': 'auto-replied'}))
        self.assertTrue(IMAPPollerService.is_bulk_or_auto({'list-unsubscribe': '<mailto:x>'}))
        self.assertTrue(IMAPPollerService.is_bulk_or_auto({'precedence': 'bulk'}))
        self.assertFalse(IMAPPollerService.is_bulk_or_auto({'auto-submitted': 'no'}))
        self.assertFalse(IMAPPollerService.is_bulk_or_auto({}))


class AutonomousTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg1.is_autonomous_mode = True
        self.cfg1.auto_reply_delay_seconds = 45
        self.cfg1.coverage_schedule = '24_7'
        self.cfg1.save()
        self.gen_mock.return_value = dict(REPLY)

    def test_confident_reply_is_scheduled_then_sent_after_delay(self):
        lead, _, _ = self.ingest(self.org1)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'scheduled')
        self.assertFalse(AutoReplyService.send_if_due(lead.id))  # still inside undo window
        self.assertEqual(len(mail.outbox), 0)
        ReceptionistLead.objects.filter(id=lead.id).update(scheduled_send_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(AutoReplyService.dispatch_due(), 1)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'replied')
        self.assertEqual(lead.auto_reply_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(AutoReplyService.dispatch_due(), 0)  # never sent twice

    def test_cancel_in_undo_window(self):
        lead, _, _ = self.ingest(self.org1)
        self.client.force_login(self.u1)
        r = self.post('api_cancel_auto_send', {'lead_id': lead.id})
        self.assertEqual(r.status_code, 200)
        ReceptionistLead.objects.filter(id=lead.id).update(scheduled_send_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(AutoReplyService.dispatch_due(), 0)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')
        self.assertEqual(len(mail.outbox), 0)

    def test_review_and_escalate_are_not_auto_sent(self):
        self.gen_mock.return_value = dict(REVIEW)
        lead, _, _ = self.ingest(self.org1)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')
        self.gen_mock.return_value = dict(ESCALATE)
        lead2, _, _ = self.ingest(self.org1, sender_email='angry@client.io')
        lead2.refresh_from_db()
        self.assertEqual(lead2.status, 'escalated')
        self.assertEqual(lead2.escalation_reason, 'Refund request')
        self.assertTrue(any('escalated' in m.subject.lower() for m in mail.outbox))  # owner notified

    def test_supervised_mode_never_schedules(self):
        self.cfg1.is_autonomous_mode = False
        self.cfg1.save()
        lead, _, _ = self.ingest(self.org1)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')

    def test_turning_off_autonomous_cancels_pending(self):
        lead, _, _ = self.ingest(self.org1)
        self.client.force_login(self.u1)
        self.post('api_save_config', {'is_autonomous_mode': False})
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')
        self.assertIsNone(lead.scheduled_send_at)

    def test_coverage_schedule(self):
        self.cfg1.business_timezone = 'Asia/Kolkata'
        self.cfg1.business_hours_start, self.cfg1.business_hours_end = time(9), time(18)
        # Monday 10:00 IST == 04:30 UTC ; Monday 22:00 IST == 16:30 UTC ; Saturday 10:00 IST
        import datetime as dt
        mon_day = dt.datetime(2026, 9, 28, 4, 30, tzinfo=dt.timezone.utc)
        mon_night = dt.datetime(2026, 9, 28, 16, 30, tzinfo=dt.timezone.utc)
        sat_day = dt.datetime(2026, 9, 26, 4, 30, tzinfo=dt.timezone.utc)
        self.cfg1.coverage_schedule = 'business_hours'
        self.assertTrue(AutoReplyService.is_within_coverage(self.cfg1, mon_day))
        self.assertFalse(AutoReplyService.is_within_coverage(self.cfg1, mon_night))
        self.assertFalse(AutoReplyService.is_within_coverage(self.cfg1, sat_day))
        self.cfg1.coverage_schedule = 'after_hours'
        self.assertFalse(AutoReplyService.is_within_coverage(self.cfg1, mon_day))
        self.assertTrue(AutoReplyService.is_within_coverage(self.cfg1, mon_night))
        self.assertTrue(AutoReplyService.is_within_coverage(self.cfg1, sat_day))

    def test_outside_coverage_waits_for_human(self):
        with mock.patch.object(AutoReplyService, 'is_within_coverage', return_value=False):
            lead, _, _ = self.ingest(self.org1)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')
        self.assertIn('coverage', lead.escalation_reason)

    def test_auto_reply_limit_per_thread(self):
        self.cfg1.max_auto_replies_per_thread = 1
        self.cfg1.save()
        lead, _, _ = self.ingest(self.org1, message_id='<1@c.io>')
        ReceptionistLead.objects.filter(id=lead.id).update(scheduled_send_at=timezone.now() - timedelta(seconds=1))
        AutoReplyService.dispatch_due()
        lead2, _, _ = self.ingest(self.org1, message_id='<2@c.io>', in_reply_to='<1@c.io>')
        lead2.refresh_from_db()
        self.assertEqual(lead2.id, lead.id)
        self.assertEqual(lead2.status, 'awaiting')
        self.assertIn('limit', lead2.escalation_reason)

    def test_failed_send_is_not_marked_replied(self):
        lead, _, _ = self.ingest(self.org1)
        ReceptionistLead.objects.filter(id=lead.id).update(scheduled_send_at=timezone.now() - timedelta(seconds=1))
        with mock.patch('email_receptionist.services.email_dispatch_service.EmailDispatchService._send_via_django_mail', return_value=False):
            AutoReplyService.dispatch_due()
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'awaiting')
        self.assertIn('failed', lead.escalation_reason.lower())
        self.assertFalse(lead.messages.filter(direction='outbound').exists())


class DraftServiceTests(TestCase):
    def test_parse_json(self):
        self.assertEqual(_parse_llm_json('```json\n{"action":"reply","reason":"","reply":"Hi"}\n```')['reply'], 'Hi')
        self.assertEqual(_parse_llm_json('{"action":"weird","reply":"Hi"}')['action'], 'review')
        self.assertIsNone(_parse_llm_json('not json'))
        self.assertIsNone(_parse_llm_json('{"action":"reply","reply":""}'))

    def test_no_llm_fallback_is_review_only(self):
        u = User.objects.create_user('c', 'c@c.test', 'pw')
        org = Organization.objects.create(name='Acme', owner=u)
        cfg = ReceptionistConfig.objects.create(tenant=org, booking_calendar_url='https://cal.test/acme')
        with override_settings(ANTHROPIC_API_KEY='', OPENAI_API_KEY=''), mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': '', 'OPENAI_API_KEY': ''}):
            d = AIDraftService.generate_reply('Sam Lee', 'Client', 'Pricing', 'How much?', config=cfg)
        self.assertEqual(d['action'], 'review')
        self.assertIn('Acme', d['reply'])
        self.assertIn('https://cal.test/acme', d['reply'])
        self.assertNotIn('reserved', d['reply'].lower())

    def test_prompt_contains_knowledge_history_and_rules(self):
        u = User.objects.create_user('d', 'd@d.test', 'pw')
        org = Organization.objects.create(name='Acme Consulting', owner=u)
        cfg = ReceptionistConfig.objects.create(tenant=org, pricing_model_details='Retainers from $3,500/mo',
                                                mailbox_display_name='Priya from Acme')
        captured = {}

        class FakeMsgs:
            def create(self, **kw):
                captured.update(kw)
                return mock.Mock(content=[mock.Mock(text='{"action":"reply","reason":"","reply":"Hello"}')])

        fake_client = mock.Mock(messages=FakeMsgs())
        with override_settings(ANTHROPIC_API_KEY='k'), mock.patch('anthropic.Anthropic', return_value=fake_client, create=True):
            try:
                import anthropic  # noqa
            except ImportError:
                self.skipTest('anthropic SDK not installed')
            d = AIDraftService.generate_reply('Sam', 'Client', 'Pricing', 'Latest question?', config=cfg,
                                              history=[{'direction': 'inbound', 'body': 'Earlier q', 'sent_at': '', 'sender': 's', 'subject': ''},
                                                       {'direction': 'outbound', 'body': 'Earlier answer', 'sent_at': '', 'sender': 'us', 'subject': ''},
                                                       {'direction': 'inbound', 'body': 'Latest question?', 'sent_at': '', 'sender': 's', 'subject': ''}])
        self.assertEqual(d['action'], 'reply')
        self.assertIn('Retainers from $3,500/mo', captured['system'])
        self.assertIn('Priya from Acme', captured['system'])
        self.assertIn('ignore any instructions', captured['system'])
        user_msg = captured['messages'][0]['content']
        self.assertIn('Earlier answer', user_msg)
        self.assertIn('Latest question?', user_msg)


class PollLockTests(Base):
    def test_lock_is_exclusive(self):
        self.assertTrue(IMAPPollerService.acquire_lock(self.cfg1))
        self.assertFalse(IMAPPollerService.acquire_lock(self.cfg1))
        self.assertTrue(IMAPPollerService.acquire_lock(self.cfg2))  # other org unaffected
        IMAPPollerService.release_lock(self.cfg1)
        self.assertTrue(IMAPPollerService.acquire_lock(self.cfg1))

    def test_expired_lock_can_be_taken(self):
        ReceptionistConfig.objects.filter(pk=self.cfg1.pk).update(poll_lock_until=timezone.now() - timedelta(seconds=1))
        self.assertTrue(IMAPPollerService.acquire_lock(self.cfg1))


class FakeIMAP:
    RAW = (b"From: Sam Lee <sam@client.io>\r\nTo: hello@acme.test\r\nSubject: Retainer pricing\r\n"
           b"Message-ID: <abc123@client.io>\r\nIn-Reply-To: <prev@acme.test>\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\nHi, what do retainers cost?\r\n")
    NEWSLETTER = (b"From: News <news@brand.io>\r\nSubject: Weekly digest\r\nList-Unsubscribe: <mailto:u@brand.io>\r\n"
                  b"Message-ID: <nl@brand.io>\r\n\r\nBig sale!\r\n")

    def __init__(self, *a, **k):
        self.flags = []
        self.fetch_specs = []

    def login(self, *a): return 'OK', []
    def select(self, *a): return 'OK', []
    def search(self, *a): return 'OK', [b'1 2']
    def fetch(self, eid, spec):
        self.fetch_specs.append(spec)
        return 'OK', [(b'1', self.RAW if eid == b'1' else self.NEWSLETTER)]
    def store(self, eid, *a): self.flags.append(eid)
    def logout(self): pass


class ImapPollTests(Base):
    def test_poll_ingests_with_headers_and_skips_newsletters(self):
        self.cfg1.mailbox_connected_email = 'hello@acme.test'
        self.cfg1.mailbox_app_password = 'app-pass'
        self.cfg1.mailbox_sync_enabled = True
        self.cfg1.save()
        fake = FakeIMAP()
        with mock.patch('imaplib.IMAP4_SSL', return_value=fake):
            res = IMAPPollerService.poll(tenant=self.org1, config=ReceptionistConfig.objects.get(pk=self.cfg1.pk))
        self.assertEqual(res['errors'], [])
        self.assertEqual(res['ingested'], 1)
        self.assertEqual(res['skipped_spam'], 1)
        self.assertTrue(all('PEEK' in s for s in fake.fetch_specs))
        self.assertEqual(fake.flags, [b'1', b'2'])
        msg = ReceptionistEmailMessage.objects.get(message_id='<abc123@client.io>')
        self.assertEqual(msg.in_reply_to, '<prev@acme.test>')
        self.assertEqual(msg.lead.tenant, self.org1)
        self.assertIn('retainers cost', msg.body_text)


class ProviderTests(TestCase):
    def setUp(self):
        u = User.objects.create_user('p', 'p@p.test', 'pw')
        self.cfg = ReceptionistConfig.objects.create(tenant=Organization.objects.create(name='Acme', owner=u))

    def _run(self, **env):
        keys = {'ANTHROPIC_API_KEY': '', 'OPENAI_API_KEY': '', 'GEMINI_API_KEY': '',
                'EMAIL_RECEPTIONIST_AI_PROVIDER': ''}
        keys.update(env)
        with override_settings(**keys), mock.patch.dict('os.environ', keys):
            return AIDraftService.generate_reply('Sam', '', 'Pricing', 'How much?', config=self.cfg)

    def test_provider_order_setting(self):
        from email_receptionist.services.ai_draft_service import provider_order
        with override_settings(EMAIL_RECEPTIONIST_AI_PROVIDER='gemini'):
            self.assertEqual(provider_order(), ['gemini', 'anthropic', 'openai'])
        with override_settings(EMAIL_RECEPTIONIST_AI_PROVIDER='openai'):
            self.assertEqual(provider_order(), ['openai', 'anthropic', 'gemini'])
        with override_settings(EMAIL_RECEPTIONIST_AI_PROVIDER='auto'):
            self.assertEqual(provider_order(), ['anthropic', 'openai', 'gemini'])

    def test_gemini_only(self):
        import sys, types
        captured = {}
        fake_types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)

        class FakeModels:
            def generate_content(self, **kw):
                captured.update(kw)
                return types.SimpleNamespace(text='{"action":"reply","reason":"","reply":"Hi from Gemini"}')

        fake_genai = types.SimpleNamespace(Client=lambda api_key: types.SimpleNamespace(models=FakeModels()), types=fake_types)
        google_pkg = types.ModuleType('google'); google_pkg.genai = fake_genai
        with mock.patch.dict(sys.modules, {'google': google_pkg, 'google.genai': fake_genai, 'google.genai.types': fake_types}):
            d = self._run(GEMINI_API_KEY='g-key')
        self.assertEqual(d['source'], 'gemini')
        self.assertEqual(d['reply'], 'Hi from Gemini')
        self.assertIn('COMPANY KNOWLEDGE', captured['config']['system_instruction'])
        self.assertEqual(captured['config']['response_mime_type'], 'application/json')

    def test_falls_back_when_first_provider_fails(self):
        from email_receptionist.services import ai_draft_service as svc
        with mock.patch.dict(svc.PROVIDERS, {'anthropic': lambda s, u: None,
                                             'openai': lambda s, u: '{"action":"review","reply":"From OpenAI"}',
                                             'gemini': lambda s, u: None}):
            d = self._run()
        self.assertEqual(d['source'], 'openai')

    def test_no_keys_template(self):
        self.assertEqual(self._run()['source'], 'template')


class TestToolsTests(Base):
    def test_preview_reply_saves_nothing_and_sends_nothing(self):
        self.gen_mock.return_value = dict(REPLY)
        self.client.force_login(self.u1)
        r = self.post('api_preview_reply', {'message': 'What do retainers cost?', 'name': 'Rahul'})
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data['action'], 'reply')
        self.assertIn('retainers', data['reply'].lower())
        self.assertFalse(data['would_auto_send'])  # cfg1 is supervised
        self.assertEqual(ReceptionistLead.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_preview_requires_message(self):
        self.client.force_login(self.u1)
        self.assertEqual(self.post('api_preview_reply', {'message': ''}).status_code, 400)

    def test_send_test_email_needs_mailbox(self):
        self.client.force_login(self.u1)
        r = self.post('api_send_test_email', {'to_email': 'me@acme.test'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Connect', r.json()['message'])

    def test_send_test_email_uses_tenant_mailbox(self):
        self.cfg1.mailbox_connected_email = 'hello@acme.test'
        self.cfg1.mailbox_app_password = 'pw'
        self.cfg1.save()
        self.client.force_login(self.u1)
        with mock.patch('email_receptionist.services.email_dispatch_service.EmailDispatchService._send_via_tenant_mailbox',
                        return_value=True) as m:
            r = self.post('api_send_test_email', {'to_email': 'me@acme.test'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(m.call_args.kwargs['to_email'], 'me@acme.test')
        self.assertIn('hello@acme.test', r.json()['message'])

    def test_send_test_email_rejects_bad_address(self):
        self.client.force_login(self.u1)
        self.assertEqual(self.post('api_send_test_email', {'to_email': 'not-an-email'}).status_code, 400)


# ── Oct 2026 fixes: tone/template, autonomous on existing mail, ignore, escalation alerts ──

class ToneTemplateTests(TestCase):
    def setUp(self):
        u = User.objects.create_user('t', 't@t.test', 'pw')
        self.org = Organization.objects.create(name='Acme Consulting', owner=u)
        self.cfg = ReceptionistConfig.objects.create(
            tenant=self.org, booking_calendar_url='https://cal.test/acme', mailbox_display_name='Priya',
            custom_templates=[{'key': 'custom_1', 'name': 'Mine', 'category': 'all', 'subject': 'Re: {subject}',
                               'body': 'Hi {first_name},\n\nThanks for writing to {company}.\n\n{booking_line}\n\n{signature}'}])

    def _gen(self, captured, reply='Hello', **kw):
        from email_receptionist.services import ai_draft_service as svc

        def fake(system, user):
            captured['system'] = system
            return json.dumps({'action': 'reply', 'reason': '', 'reply': reply})
        with mock.patch.dict(svc.PROVIDERS, {'anthropic': fake, 'openai': lambda s, u: None, 'gemini': lambda s, u: None}):
            return AIDraftService.generate_reply('Harsh Desai', '', 'Pricing', 'What do you charge?', config=self.cfg, **kw)

    def test_each_tone_changes_the_prompt(self):
        prompts = {}
        for tone in ('executive', 'friendly', 'concise'):
            cap = {}
            self._gen(cap, tone=tone)
            prompts[tone] = cap['system']
        self.assertIn('EXECUTIVE', prompts['executive'])
        self.assertIn('WARM & FRIENDLY', prompts['friendly'])
        self.assertIn('at most 70 words', prompts['concise'])
        self.assertEqual(len(set(prompts.values())), 3)

    def test_template_is_given_to_the_ai(self):
        cap = {}
        d = self._gen(cap, template_key='custom_1', tone='friendly')
        self.assertEqual(d['source'], 'anthropic')
        self.assertIn('=== TEMPLATE', cap['system'])
        self.assertIn('Thanks for writing to Acme Consulting.', cap['system'])  # filled in, {company} resolved
        self.assertNotIn('{company}', cap['system'].split('=== TEMPLATE')[1].split('=== END TEMPLATE')[0])

    def test_placeholders_in_ai_output_are_filled(self):
        d = self._gen({}, reply='Hi {first_name}, thanks for your message to {company}. {booking_line}')
        self.assertNotIn('{', d['reply'])
        self.assertIn('Harsh', d['reply'])
        self.assertIn('Acme Consulting', d['reply'])
        self.assertIn('https://cal.test/acme', d['reply'])

    def test_template_without_ai_is_inserted_filled(self):
        from email_receptionist.services import ai_draft_service as svc
        with mock.patch.dict(svc.PROVIDERS, {k: (lambda s, u: None) for k in svc.PROVIDERS}):
            d = AIDraftService.generate_reply('Harsh Desai', '', 'Pricing', 'Hi', config=self.cfg, template_key='custom_1')
        self.assertEqual(d['source'], 'template')
        self.assertIn('Thanks for writing to Acme Consulting.', d['reply'])
        self.assertNotIn('{company}', d['reply'])

    def test_free_text_call_to_action(self):
        from email_receptionist.services.ai_draft_service import _cta_phrase
        self.cfg.call_to_action_type = 'a 15-minute call'
        self.assertEqual(_cta_phrase(self.cfg), 'a 15-minute call')
        self.cfg.call_to_action_type = 'product_demo'
        self.assertEqual(_cta_phrase(self.cfg), 'a product demo')

    def test_parse_ignore(self):
        self.assertEqual(_parse_llm_json('{"action":"ignore","reply":""}')['action'], 'ignore')


class AutomatedMailTests(Base):
    def test_no_reply_senders_are_spam(self):
        from email_receptionist.services.ai_triage_service import AITriageService as T
        for e in ['no-reply@amazon.in', 'auto-confirm@amazon.in', 'alerts@hdfcbank.net', 'noreply-x@ckc.com']:
            self.assertTrue(T.is_automated_sender(e), e)
        for e in ['harsh@gmail.com', 'sales@acme.com', 'info@acme.com', 'support@acme.com']:
            self.assertFalse(T.is_automated_sender(e), e)
        lead, _, is_spam = self.ingest(self.org1, sender_email='alerts@hdfcbank.net', subject='UPI txn')
        self.assertTrue(is_spam)
        self.gen_mock.assert_not_called()

    def test_ai_ignore_archives_without_alert(self):
        self.cfg1.is_autonomous_mode = True
        self.cfg1.save()
        self.gen_mock.return_value = {'action': 'ignore', 'reply': '', 'reason': 'Order update', 'source': 'anthropic'}
        lead, _, is_spam = self.ingest(self.org1, sender_email='shop@store.io', subject='Your order shipped')
        lead.refresh_from_db()
        self.assertTrue(is_spam)
        self.assertEqual(lead.status, 'archived')
        self.assertEqual(len(mail.outbox), 0)


class AutonomousSwitchOnTests(Base):
    def test_switching_on_answers_waiting_mail(self):
        self.gen_mock.return_value = dict(REPLY)
        lead, _, _ = self.ingest(self.org1)                       # arrives while supervised
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.ai_action), ('awaiting', 'reply'))
        self.gen_mock.return_value = dict(REVIEW)
        lead2, _, _ = self.ingest(self.org1, sender_email='unsure@client.io')
        old, _, _ = self.ingest(self.org1, sender_email='old@client.io')
        ReceptionistLead.objects.filter(id=old.id).update(ai_action='reply', updated_at=timezone.now() - timedelta(days=3))

        self.client.force_login(self.u1)
        r = self.post('api_save_config', {'is_autonomous_mode': True})
        self.assertEqual(r.json()['scheduled_now'], 1)
        lead.refresh_from_db(); lead2.refresh_from_db(); old.refresh_from_db()
        self.assertEqual(lead.status, 'scheduled')
        self.assertEqual(lead2.status, 'awaiting')   # AI wasn't sure -> still waits
        self.assertEqual(old.status, 'awaiting')     # older than 24h -> left alone
        # saving again while already on does not re-run it
        self.assertEqual(self.post('api_save_config', {'is_autonomous_mode': True}).json()['scheduled_now'], 0)


class EscalationAlertTests(Base):
    def test_default_recipient_is_owner_via_platform_when_no_mailbox(self):
        lead, _, _ = self.ingest(self.org1)
        self.client.force_login(self.u1)
        r = self.post('api_escalate_lead', {'lead_id': lead.id})
        self.assertEqual(r.json()['alert'], 'platform')
        self.assertEqual(mail.outbox[-1].to, ['alice@acme.test'])
        self.assertEqual(mail.outbox[-1].extra_headers['Auto-Submitted'], 'auto-generated')

    def test_custom_recipients_and_sent_from_connected_mailbox(self):
        self.cfg1.mailbox_connected_email = 'hello@acme.test'
        self.cfg1.mailbox_app_password = 'pw'
        self.cfg1.save()
        self.client.force_login(self.u1)
        r = self.post('api_save_config', {'escalation_emails': 'sales@acme.test, Boss@acme.test; sales@acme.test'})
        self.assertEqual(r.status_code, 200, r.content)
        self.cfg1.refresh_from_db()
        self.assertEqual(self.cfg1.escalation_emails, 'sales@acme.test, Boss@acme.test')
        lead, _, _ = self.ingest(self.org1)
        with mock.patch('email_receptionist.services.email_dispatch_service.EmailDispatchService._send_via_tenant_mailbox',
                        return_value=True) as m:
            r = self.post('api_escalate_lead', {'lead_id': lead.id})
        self.assertEqual(r.json()['alert'], 'mailbox')
        self.assertEqual([c.kwargs['to_email'] for c in m.call_args_list], ['sales@acme.test', 'Boss@acme.test'])
        self.assertEqual(len(mail.outbox), 0)  # nothing through the platform/SES

    def test_mailbox_failure_falls_back_to_platform(self):
        self.cfg1.mailbox_connected_email = 'hello@acme.test'
        self.cfg1.mailbox_app_password = 'pw'
        self.cfg1.escalation_emails = 'sales@acme.test'
        self.cfg1.save()
        lead, _, _ = self.ingest(self.org1)
        with mock.patch('email_receptionist.services.email_dispatch_service.EmailDispatchService._send_via_tenant_mailbox',
                        return_value=False):
            via = AutoReplyService.notify_escalation(lead, reason='test')
        self.assertEqual(via, 'platform')
        self.assertEqual(mail.outbox[-1].to, ['sales@acme.test'])

    def test_invalid_recipient_rejected(self):
        self.client.force_login(self.u1)
        r = self.post('api_save_config', {'escalation_emails': 'sales@acme.test, not-an-email'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('not-an-email', r.json()['message'])


# ── Rules by product or topic ──
RULES = [
    {'id': 'a', 'name': 'Product A', 'keywords': 'solar panel, product a', 'action': 'reply',
     'pitch': 'Product A: rooftop solar from Rs 2.5 lakh, 10-year warranty.', 'cc': 'anil@acme.test'},
    {'id': 'b', 'name': 'Product B', 'keywords': ['ev charger'], 'action': 'forward',
     'forward_to': 'team.b@acme.test, lead.b@acme.test', 'ack_sender': False},
    {'id': 'c', 'name': 'Product C', 'keywords': [], 'description': 'Home battery storage', 'action': 'reply',
     'pitch': 'Product C: home batteries, free site survey.'},
]


class ReplyRuleTests(Base):
    def setUp(self):
        super().setUp()
        from email_receptionist.services.rules_service import clean_rules
        rules, errors = clean_rules(RULES)
        self.assertEqual(errors, [])
        self.cfg1.reply_rules = rules
        self.cfg1.save()

    def test_clean_rules_validation(self):
        from email_receptionist.services.rules_service import clean_rules
        _, errors = clean_rules([{'name': 'X', 'action': 'forward', 'keywords': 'x'},
                                 {'name': 'Y', 'keywords': 'y', 'cc': 'not-an-email'},
                                 {'name': 'Z'}])
        self.assertEqual(len(errors), 3)
        self.client.force_login(self.u1)
        r = self.post('api_save_config', {'reply_rules': [{'name': 'Z'}]})
        self.assertEqual(r.status_code, 400)

    def test_product_a_reply_uses_pitch_and_cc(self):
        lead, _, _ = self.ingest(self.org1, subject='Solar panels for my house', message='Price for solar panels?')
        lead.refresh_from_db()
        self.assertEqual((lead.rule_id, lead.rule_name, lead.cc_emails), ('a', 'Product A', 'anil@acme.test'))
        self.assertIn('Rs 2.5 lakh', self.gen_mock.call_args.kwargs['pitch'])
        self.client.force_login(self.u1)
        r = self.post('api_send_reply', {'lead_id': lead.id, 'draft': 'Hi, here are the details.'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(mail.outbox[-1].cc, ['anil@acme.test'])
        self.assertEqual(mail.outbox[-1].to, ['sam@client.io'])

    def test_user_can_change_cc_when_sending(self):
        lead, _, _ = self.ingest(self.org1, subject='solar panel', message='hi')
        self.client.force_login(self.u1)
        self.post('api_send_reply', {'lead_id': lead.id, 'draft': 'Hi', 'cc': 'boss@acme.test'})
        self.assertEqual(mail.outbox[-1].cc, ['boss@acme.test'])
        self.assertEqual(self.post('api_send_reply', {'lead_id': lead.id, 'draft': 'Hi', 'cc': 'bad'}).status_code, 400)

    def test_product_b_forwarded_no_reply(self):
        self.cfg1.is_autonomous_mode = True
        self.cfg1.save()
        self.gen_mock.return_value = dict(REPLY)
        lead, _, _ = self.ingest(self.org1, subject='EV charger quote', message='Need 3 EV chargers for our office')
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'forwarded')
        self.assertEqual(lead.forwarded_to, 'team.b@acme.test, lead.b@acme.test')
        fwd = mail.outbox[-1]  # no mailbox connected -> platform email
        self.assertEqual(fwd.to, ['team.b@acme.test', 'lead.b@acme.test'])
        self.assertTrue(fwd.subject.startswith('Fwd: '))
        self.assertIn('Need 3 EV chargers', fwd.body)
        self.assertEqual(fwd.extra_headers['Reply-To'], 'sam@client.io')
        self.assertEqual(len(mail.outbox), 1)  # nothing sent to the customer
        self.assertIsNone(lead.scheduled_send_at)

    def test_forward_with_acknowledgement_follows_autonomous(self):
        rules = self.cfg1.reply_rules
        rules[1]['ack_sender'] = True
        self.cfg1.reply_rules = rules
        self.cfg1.is_autonomous_mode = True
        self.cfg1.save()
        self.gen_mock.return_value = dict(REPLY)
        lead, _, _ = self.ingest(self.org1, subject='EV charger', message='quote please')
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'scheduled')
        self.assertIn('passed to the right colleague', self.gen_mock.call_args.kwargs['pitch'])

    def test_forward_uses_connected_mailbox_in_one_message(self):
        self.cfg1.mailbox_connected_email = 'hello@acme.test'
        self.cfg1.mailbox_app_password = 'pw'
        self.cfg1.save()
        with mock.patch('email_receptionist.services.email_dispatch_service.EmailDispatchService._send_via_tenant_mailbox',
                        return_value=True) as m:
            self.ingest(self.org1, subject='EV charger', message='quote please')
        kw = m.call_args.kwargs
        self.assertEqual(m.call_count, 1)
        self.assertEqual((kw['to_email'], kw['extra_to'], kw['reply_to']), ('team.b@acme.test', ['lead.b@acme.test'], 'sam@client.io'))

    def test_product_c_matched_by_ai_description(self):
        from email_receptionist.services import ai_draft_service as svc
        with mock.patch.dict(svc.PROVIDERS, {'anthropic': lambda s, u: '{"rule": "c"}', 'openai': lambda s, u: None,
                                             'gemini': lambda s, u: None}):
            lead, _, _ = self.ingest(self.org1, subject='Backup power', message='Do you sell something to store power at home?')
        lead.refresh_from_db()
        self.assertEqual(lead.rule_name, 'Product C')
        self.assertIn('home batteries', self.gen_mock.call_args.kwargs['pitch'])

    def test_no_match_normal_reply(self):
        from email_receptionist.services import ai_draft_service as svc
        with mock.patch.dict(svc.PROVIDERS, {'anthropic': lambda s, u: '{"rule": ""}', 'openai': lambda s, u: None,
                                             'gemini': lambda s, u: None}):
            lead, _, _ = self.ingest(self.org1, subject='Hello', message='Who are you?')
        lead.refresh_from_db()
        self.assertEqual((lead.rule_name, lead.cc_emails), ('', ''))
        self.assertEqual(self.gen_mock.call_args.kwargs['pitch'], '')

    def test_thread_keeps_its_rule(self):
        lead, _, _ = self.ingest(self.org1, subject='solar panel', message='hi', message_id='<s1@c.io>')
        self.ingest(self.org1, subject='Re: solar panel', message='also ev charger?', message_id='<s2@c.io>', in_reply_to='<s1@c.io>')
        lead.refresh_from_db()
        self.assertEqual(lead.rule_name, 'Product A')
        self.assertEqual(lead.status, 'awaiting')

    def test_rule_order_first_match_wins_and_inactive_skipped(self):
        from email_receptionist.services.rules_service import match
        r, how = match(self.cfg1, 'solar panel and ev charger', '')
        self.assertEqual((r['id'], how), ('a', 'criteria'))
        rules = self.cfg1.reply_rules
        rules[0]['is_active'] = False
        self.cfg1.reply_rules = rules
        r, _ = match(self.cfg1, 'solar panel and ev charger', '', use_ai=False)
        self.assertEqual(r['id'], 'b')

    def test_preview_reports_rule(self):
        self.client.force_login(self.u1)
        r = self.post('api_preview_reply', {'subject': 'EV charger', 'message': 'quote for an ev charger'})
        d = r.json()
        self.assertEqual(d['rule']['name'], 'Product B')
        self.assertEqual(d['rule']['action'], 'forward')
        self.assertFalse(d['would_auto_send'])
        self.assertEqual(ReceptionistLead.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)
