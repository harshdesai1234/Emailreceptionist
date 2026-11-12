"""
Management Command: poll_mailbox
=================================
Polls connected mailboxes and sends any due autonomous replies.
In production this runs automatically through Celery beat
(email_receptionist.tasks.poll_all_mailboxes_task / dispatch_due_replies_task);
this command is for manual runs and local development without Celery.

Usage:
  python manage.py poll_mailbox                       # poll once
  python manage.py poll_mailbox --tenant 1            # only organization pk=1
  python manage.py poll_mailbox --loop --interval 60  # keep polling
"""
import time

from django.core.management.base import BaseCommand

from email_receptionist.services.auto_reply_service import AutoReplyService
from email_receptionist.services.imap_poller_service import IMAPPollerService


class Command(BaseCommand):
    help = 'Polls connected mailboxes, ingests new emails and sends due autonomous replies.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=int, default=None, help='Organization primary key to poll.')
        parser.add_argument('--loop', action='store_true', help='Run continuously.')
        parser.add_argument('--interval', type=int, default=60, help='Seconds between polls with --loop.')

    def handle(self, *args, **options):
        while True:
            totals = IMAPPollerService.poll_all(tenant_pk=options.get('tenant'))
            sent = AutoReplyService.dispatch_due()
            self.stdout.write(
                f"Mailboxes: {totals['mailboxes']} | Ingested: {totals['ingested']} | "
                f"Spam skipped: {totals['skipped_spam']} | Auto-replies sent: {sent}"
            )
            for err in totals['errors']:
                self.stdout.write(self.style.ERROR(f'  ✗ {err}'))
            if not options.get('loop'):
                break
            time.sleep(max(10, options.get('interval') or 60))
