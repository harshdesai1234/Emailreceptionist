from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta

from accounts.models import Organization, UserProfile
from email_receptionist.models import (
    ReceptionistConfig,
    ReceptionistLead,
    ReceptionistEmailMessage,
    generate_webhook_token,
)


class Command(BaseCommand):
    help = 'Seeds a default demo admin user, organization, configuration, and sample leads.'

    def handle(self, *args, **options):
        self.stdout.write("Setting up demo data for Email Receptionist...")

        # 1. User
        user, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'admin@agentyne.com',
                'first_name': 'Dev',
                'last_name': 'Admin',
                'is_staff': True,
                'is_superuser': True,
            }
        )
        user.set_password('admin123')
        user.is_staff = True
        user.is_superuser = True
        user.save()
        self.stdout.write(self.style.SUCCESS(f"User: admin (password: admin123)"))

        # 2. Organization
        org, org_created = Organization.objects.get_or_create(
            owner=user,
            defaults={
                'name': 'Acme Innovations',
                'customer_id': 'AGT-DEMO01',
                'is_active': True,
            }
        )
        if not org_created and org.customer_id != 'AGT-DEMO01':
            org.customer_id = 'AGT-DEMO01'
            org.save(update_fields=['customer_id'])

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.organization = org
        profile.role = 'owner'
        profile.save()
        self.stdout.write(self.style.SUCCESS(f"Organization: {org.name} ({org.customer_id})"))

        # 3. ReceptionistConfig
        config, _ = ReceptionistConfig.objects.get_or_create(
            tenant=org,
            defaults={
                'is_autonomous_mode': True,
                'default_tone': 'executive',
                'auto_reply_delay_seconds': 45,
                'coverage_schedule': '24_7',
                'business_timezone': 'America/New_York',
                'business_type': 'service',
                'company_tagline': 'Agentyne automates customer operations and 24/7 AI email reception.',
                'services_offered': (
                    "- Autonomous 24/7 inbound email reception and triage.\n"
                    "- Automated qualification and meeting scheduling.\n"
                    "- Custom enterprise workflow integrations."
                ),
                'pricing_model_details': (
                    "- Starter tier: $1,500/mo (up to 500 emails/mo).\n"
                    "- Growth tier: $3,500/mo (up to 2,500 emails/mo with autonomous sending).\n"
                    "- Custom Enterprise: Tailored quote upon scoping call."
                ),
                'company_faq_guardrails': (
                    "- All client data is encrypted at rest and in transit.\n"
                    "- Never invent custom pricing discounts without approval.\n"
                    "- Standard delivery turnaround: 2-3 weeks for custom deployments."
                ),
                'booking_calendar_url': 'https://calendly.com/agentyne-demo/30min',
                'email_provider': 'smtp_imap',
                'mailbox_connected_email': 'inbox@acme-innovations.com',
                'mailbox_display_name': 'Acme AI Receptionist',
                'mailbox_sync_enabled': False,
                'webhook_token': generate_webhook_token(),
            }
        )
        self.stdout.write(self.style.SUCCESS("Receptionist Configuration created/updated."))

        # 4. Sample Leads
        now = timezone.now()
        leads_data = [
            {
                'sender_name': 'Sarah Jenkins',
                'sender_email': 'sarah.jenkins@northwind.io',
                'company_name': 'Northwind Corp',
                'role': 'VP of Operations',
                'team_size': '50-100',
                'subject': 'Inbound Inquiry: AI Email Receptionist Pricing',
                'inbound_message': (
                    "Hello team,\n\nWe receive roughly 400 customer inquiries a week and want to automate "
                    "our first-touch replies and demo bookings. What are your pricing tiers for an agency our size?"
                ),
                'ai_draft_response': (
                    "Hello Sarah,\n\nThank you for reaching out to Acme Innovations! "
                    "For your volume of ~400 inquiries per week, our Growth package ($3,500/mo) is typically "
                    "the ideal fit, offering full autonomous coverage and intelligent triage.\n\n"
                    "You can book a short discovery call with our team here: https://calendly.com/agentyne-demo/30min\n\n"
                    "Kind regards,\nAcme AI Receptionist\nAcme Innovations"
                ),
                'status': 'awaiting',
                'qualification_score': 92,
                'intent_category': 'Enterprise Demo',
                'fit_reason': 'High volume agency looking for immediate automation; matches ideal client profile.',
                'lead_type': 'inbound_receptionist',
                'created_at': now - timedelta(hours=2),
            },
            {
                'sender_name': 'David Miller',
                'sender_email': 'david.miller@fintechcloud.com',
                'company_name': 'FinTech Cloud',
                'role': 'Director of IT',
                'team_size': '200+',
                'subject': 'Security and Data Privacy Verification',
                'inbound_message': (
                    "Hi,\n\nBefore we test the email receptionist, could you please confirm if email content is used "
                    "to train external AI models or stored unencrypted?"
                ),
                'ai_draft_response': (
                    "Hello David,\n\nThank you for checking in with us. All communication data is encrypted at rest "
                    "and in transit. Customer data is strictly isolated and never used to train public or foundational AI models.\n\n"
                    "We would be delighted to provide our full security overview during a brief technical discovery call: "
                    "https://calendly.com/agentyne-demo/30min\n\n"
                    "Kind regards,\nAcme AI Receptionist\nAcme Innovations"
                ),
                'status': 'scheduled',
                'scheduled_send_at': now + timedelta(seconds=35),
                'qualification_score': 88,
                'intent_category': 'Security Review',
                'fit_reason': 'IT leadership inquiry evaluating enterprise readiness.',
                'lead_type': 'inbound_receptionist',
                'created_at': now - timedelta(hours=5),
            },
            {
                'sender_name': 'Elena Rostova',
                'sender_email': 'elena@globalreach.org',
                'company_name': 'Global Reach',
                'role': 'Head of Growth',
                'team_size': '20-50',
                'subject': 'Reseller Partnership Request',
                'inbound_message': (
                    "Hi there,\n\nWe represent over 40 B2B service firms and would like to explore bundling your "
                    "AI receptionist with our onboarding packages."
                ),
                'ai_draft_response': (
                    "Hello Elena,\n\nThank you for reaching out. A reseller partnership sounds like a great mutual fit. "
                    "I have flagged this for our partnership team and they will be in touch shortly."
                ),
                'status': 'replied',
                'replied_at': now - timedelta(hours=1),
                'qualification_score': 95,
                'intent_category': 'Partnership',
                'fit_reason': 'High-multiplier distribution channel partner.',
                'lead_type': 'inbound_receptionist',
                'created_at': now - timedelta(days=1),
            },
            {
                'sender_name': 'Marcus Vance',
                'sender_email': 'marcus@solarsurge.com',
                'company_name': 'Solar Surge',
                'role': 'Accounts Manager',
                'team_size': '10-20',
                'subject': 'Billing question regarding invoice #8821',
                'inbound_message': (
                    "Hello, I noticed a duplicate line item on our latest invoice. Could someone please review "
                    "and issue a credit note?"
                ),
                'ai_draft_response': (
                    "Hello Marcus,\n\nThank you for bringing this to our attention. I have escalated this directly "
                    "to our billing department to review invoice #8821 and resolve any discrepancy promptly."
                ),
                'status': 'escalated',
                'escalation_reason': 'Invoice billing question and credit request requiring human accounts review.',
                'qualification_score': 65,
                'intent_category': 'Billing Dispute',
                'fit_reason': 'Existing customer service request.',
                'lead_type': 'inbound_receptionist',
                'created_at': now - timedelta(hours=3),
            },
        ]

        for item in leads_data:
            lead, l_created = ReceptionistLead.objects.get_or_create(
                tenant=org,
                sender_email=item['sender_email'],
                defaults=item
            )
            if l_created:
                # Add initial inbound message
                ReceptionistEmailMessage.objects.create(
                    organization=org,
                    lead=lead,
                    direction='inbound',
                    sender=item['sender_email'],
                    recipient=config.mailbox_connected_email or 'inbox@acme-innovations.com',
                    subject=item['subject'],
                    body_text=item['inbound_message'],
                    sent_at=item['created_at'],
                    message_id=f"<msg_{lead.id}_{timezone.now().timestamp()}@client.test>",
                )
                if item['status'] == 'replied':
                    ReceptionistEmailMessage.objects.create(
                        organization=org,
                        lead=lead,
                        direction='outbound',
                        sender=config.mailbox_connected_email or 'inbox@acme-innovations.com',
                        recipient=item['sender_email'],
                        subject=f"Re: {item['subject']}",
                        body_text=item['ai_draft_response'],
                        sent_at=item['replied_at'],
                        message_id=f"<reply_{lead.id}_{timezone.now().timestamp()}@acme.test>",
                    )

        self.stdout.write(self.style.SUCCESS(f"Created {len(leads_data)} sample leads with messages."))
        self.stdout.write(self.style.SUCCESS("[OK] Setup complete! You can now run `python manage.py runserver` and sign in."))
