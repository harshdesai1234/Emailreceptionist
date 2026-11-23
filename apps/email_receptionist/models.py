import secrets
from datetime import time

from django.db import models
from django.utils import timezone

from email_receptionist.fields import EncryptedTextField


def generate_webhook_token():
    return secrets.token_urlsafe(32)


class ReceptionistConfig(models.Model):
    """
    Tenant configuration and guardrails for the Automated Email Receptionist.
    """
    TONE_CHOICES = [
        ('executive', 'Executive / Professional'),
        ('friendly', 'Warm & Friendly'),
        ('concise', 'Direct & Concise'),
    ]

    SCHEDULE_CHOICES = [
        ('24_7', '24/7 Always Active'),
        ('business_hours', 'Business Hours Only'),
        ('after_hours', 'After-Hours & Weekends Only'),
    ]

    tenant = models.OneToOneField(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='email_receptionist_config'
    )
    is_autonomous_mode = models.BooleanField(
        default=True,
        help_text="When True, the AI autonomously drafts and sends email responses. When False, drafts wait for human approval."
    )
    default_tone = models.CharField(
        max_length=32,
        choices=TONE_CHOICES,
        default='executive',
        help_text="Default communication style for AI generated responses."
    )
    auto_reply_delay_seconds = models.PositiveIntegerField(
        default=45,
        help_text="Safety undo buffer in seconds before an autonomous email is dispatched."
    )
    coverage_schedule = models.CharField(
        max_length=32,
        choices=SCHEDULE_CHOICES,
        default='24_7',
        help_text="When autonomous replies are allowed. Outside this window drafts wait for a human."
    )
    business_hours_start = models.TimeField(default=time(9, 0))
    business_hours_end = models.TimeField(default=time(18, 0))
    business_timezone = models.CharField(
        max_length=64, default='Asia/Kolkata',
        help_text="IANA timezone used for the coverage schedule (e.g. Asia/Kolkata, America/New_York)."
    )
    reply_rules = models.JSONField(
        default=list, blank=True,
        help_text="Rules by product / topic: [{id, name, keywords, description, action: reply|forward, pitch, "
                  "template_key, cc, forward_to, ack_sender, is_active}] - first match wins."
    )
    escalation_emails = models.TextField(
        blank=True, default='',
        help_text="Who gets escalation alerts (comma or newline separated). Empty = the organization owner."
    )
    max_auto_replies_per_thread = models.PositiveIntegerField(
        default=3,
        help_text="Safety limit: after this many autonomous replies in one thread, a human must reply."
    )
    poll_lock_until = models.DateTimeField(
        blank=True, null=True,
        help_text="Internal: prevents two workers polling the same mailbox at once."
    )
    webhook_token = models.CharField(
        max_length=64, unique=True, null=True, blank=True, default=generate_webhook_token,
        help_text="Secret token required by the inbound lead webhook for this organization."
    )
    BUSINESS_TYPE_CHOICES = [
        ('product', 'Product / SaaS (Subscriptions & Fixed Tiers)'),
        ('service', 'Service / Agency / Consulting (Custom SOW, Retainers, Hourly)'),
        ('hybrid', 'Hybrid (Products + Custom Services)'),
    ]

    business_type = models.CharField(
        max_length=32,
        choices=BUSINESS_TYPE_CHOICES,
        default='service',
        help_text="Defines whether the business sells fixed SaaS products, bespoke services, or a hybrid model."
    )
    company_tagline = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="One-sentence description of what your company or agency delivers."
    )
    services_offered = models.TextField(
        blank=True,
        default="",
        help_text="List of core services, custom development capabilities, or consulting offerings."
    )
    pricing_model_details = models.TextField(
        blank=True,
        default="",
        help_text="Engagement & pricing models (e.g. 'Project minimum $10k', 'Retainers from $3,500/mo', 'Custom quote upon scoping')."
    )
    call_to_action_type = models.CharField(
        max_length=64,
        default="discovery_call",
        help_text="Primary meeting call to action ('discovery_call', 'scoping_meeting', 'product_demo', 'consultation')."
    )

    company_faq_guardrails = models.TextField(
        blank=True,
        default=(
            "- Custom project scopes tailored after a preliminary technical discovery call.\n"
            "- Standard engagement turnaround: 2-6 weeks depending on SOW requirements.\n"
            "- Full IP ownership transferred upon project signoff."
        ),
        help_text="Company knowledge, pricing guardrails, and FAQs used by the AI to answer inquiries accurately."
    )
    raw_business_notes = models.TextField(
        blank=True,
        default="",
        help_text="Freeform knowledge, pitch decks, FAQs, or raw business description provided by the user."
    )
    extracted_knowledge = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured synthesis extracted by the interactive knowledge assistant."
    )
    knowledge_chat_history = models.JSONField(
        default=list,
        blank=True,
        help_text="Chat messages with the onboarding / knowledge copilot."
    )
    booking_calendar_url = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Public meeting scheduling link (e.g. Google Calendar / Calendly)."
    )
    enable_spam_filter = models.BooleanField(
        default=True,
        help_text="Automatically filter out-of-office autoreplies and newsletters."
    )
    custom_templates = models.JSONField(
        default=list,
        blank=True,
        help_text="Tenant-defined custom email templates"
    )
    
    # Multi-Provider Connected Mailbox Configuration
    PROVIDER_CHOICES = [
        ('gmail', 'Google Workspace / Gmail'),
        ('outlook', 'Microsoft Outlook / Office 365'),
        ('zoho', 'Zoho Mail'),
        ('smtp_imap', 'Custom SMTP / IMAP'),
    ]
    email_provider = models.CharField(
        max_length=32,
        choices=PROVIDER_CHOICES,
        default='gmail',
        help_text="Connected mailbox provider"
    )
    mailbox_connected_email = models.EmailField(blank=True, null=True, help_text="Connected email address")
    mailbox_app_password = EncryptedTextField(blank=True, null=True, help_text="App password or API secret (encrypted)")
    mailbox_display_name = models.CharField(max_length=128, default='AI Receptionist', blank=True, null=True)
    mailbox_sync_enabled = models.BooleanField(default=False, help_text="Sync incoming inquiries via IMAP")
    
    # Custom IMAP/SMTP host & ports for enterprise setups
    imap_host = models.CharField(max_length=255, blank=True, null=True, default='imap.gmail.com')
    imap_port = models.PositiveIntegerField(default=993)
    smtp_host = models.CharField(max_length=255, blank=True, null=True, default='smtp.gmail.com')
    smtp_port = models.PositiveIntegerField(default=587)
    use_ssl_tls = models.BooleanField(default=True)

    # Google OAuth 2.0 Credentials (1-Click Account Picker)
    google_access_token = EncryptedTextField(blank=True, null=True)
    google_refresh_token = EncryptedTextField(blank=True, null=True)
    google_token_expiry = models.DateTimeField(blank=True, null=True)

    # Microsoft / Office 365 OAuth 2.0 Credentials (1-Click Account Picker)
    microsoft_access_token = EncryptedTextField(blank=True, null=True)
    microsoft_refresh_token = EncryptedTextField(blank=True, null=True)
    microsoft_token_expiry = models.DateTimeField(blank=True, null=True)

    # Zoho Mail OAuth 2.0 Credentials (1-Click Account Picker)
    zoho_access_token = EncryptedTextField(blank=True, null=True)
    zoho_refresh_token = EncryptedTextField(blank=True, null=True)
    zoho_token_expiry = models.DateTimeField(blank=True, null=True)
    zoho_accounts_server = models.CharField(max_length=128, default='https://accounts.zoho.com', blank=True, null=True)
    mailbox_last_polled_at = models.DateTimeField(null=True, blank=True, help_text='When the mailbox was last read')
    mailbox_last_error = models.TextField(blank=True, default='', help_text='Why the last read failed (empty = it worked)')

    # Legacy fields kept for backwards compatibility
    gmail_connected_email = models.EmailField(blank=True, null=True)
    gmail_app_password = EncryptedTextField(blank=True, null=True)
    gmail_sync_enabled = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Receptionist Configuration'
        verbose_name_plural = 'Receptionist Configurations'

    def __str__(self):
        return f"Receptionist Config ({self.tenant.name if self.tenant else 'Default'})"


class ReceptionistLead(models.Model):
    """
    Independent lead thread managed by the Automated Email Agent & Virtual Receptionist.
    """
    STATUS_CHOICES = [
        ('awaiting', 'Awaiting Action'),
        ('scheduled', 'Auto-Reply Scheduled'),
        ('forwarded', 'Forwarded'),
        ('replied', 'Replied'),
        ('escalated', 'Escalated to Human'),
        ('archived', 'Archived'),
    ]

    tenant = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='receptionist_leads',
        null=True,
        blank=True
    )
    sender_name = models.CharField(max_length=255, default='Anonymous Lead')
    sender_email = models.EmailField(blank=True, null=True, db_index=True)
    phone_number = models.CharField(max_length=64, blank=True, null=True)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    role = models.CharField(max_length=128, blank=True, default='')
    team_size = models.CharField(max_length=64, blank=True, default='')
    est_budget = models.CharField(max_length=64, blank=True, default='')
    source = models.CharField(max_length=128, default='Email')

    subject = models.CharField(max_length=512, default='Inbound Product Inquiry')
    inbound_message = models.TextField(blank=True, default='')
    ai_draft_response = models.TextField(blank=True, default='')
    selected_slot = models.CharField(max_length=128, blank=True, null=True)
    
    TYPE_CHOICES = [
        ('inbound_receptionist', 'AI Receptionist (Inbound)'),
        ('outbound_agent', 'Email Agent (Outbound Campaign)'),
    ]

    lead_type = models.CharField(
        max_length=32,
        choices=TYPE_CHOICES,
        default='inbound_receptionist',
        db_index=True,
        help_text="Distinguishes between Inbound AI Receptionist inquiries and Outbound Email Agent campaign sequences."
    )
    sequence_step = models.PositiveIntegerField(
        default=1,
        help_text="Sequence step number for outbound campaigns (e.g., Step 1, Step 2)."
    )

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='awaiting', db_index=True)
    qualification_score = models.PositiveIntegerField(default=85)
    intent_category = models.CharField(max_length=128, default='Enterprise Demo')
    fit_reason = models.TextField(blank=True, default="", help_text="AI-synthesized reasoning on why this prospect matches or deviates from ideal client profile.")
    enriched_profile = models.JSONField(default=dict, blank=True, help_text="Structured data parsed from email body/signature (title, phone, social, company).")
    is_spam = models.BooleanField(default=False)

    # Logistics & Freight Specific Deal Intelligence
    origin_city = models.CharField(max_length=128, blank=True, default='', help_text="Pickup location / Origin (e.g. Hubballi, Mumbai)")
    destination_city = models.CharField(max_length=128, blank=True, default='', help_text="Delivery location / Destination (e.g. Pune, Delhi)")
    cargo_type = models.CharField(max_length=255, blank=True, default='', help_text="Type of goods / cargo (e.g. Auto Spare Parts, Wooden Furniture)")
    weight_or_volume = models.CharField(max_length=128, blank=True, default='', help_text="e.g. 15 Tons, 250 kg, 40ft Container")
    load_type = models.CharField(max_length=32, blank=True, default='FTL', help_text="FTL (Full Truck Load), PTL (Part Load), Express, or Container")
    pickup_timing = models.CharField(max_length=128, blank=True, default='', help_text="Pickup urgency / schedule (e.g. Immediate / Kal Subah)")
    tracking_number = models.CharField(max_length=128, blank=True, default='', help_text="LR Number / B/L / Tracking ID if inquiry relates to existing shipment")
    est_freight_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Estimated freight quote value in INR")

    # Autonomous reply scheduling & safety
    scheduled_send_at = models.DateTimeField(blank=True, null=True, db_index=True,
        help_text="When a scheduled autonomous reply will be sent (undo window).")
    auto_reply_count = models.PositiveIntegerField(default=0,
        help_text="Autonomous replies sent in this thread since the last human reply.")
    rule_id = models.CharField(max_length=64, blank=True, default='', help_text="Matched reply rule id")
    rule_name = models.CharField(max_length=255, blank=True, default='', help_text="Matched reply rule (product / topic)")
    cc_emails = models.TextField(blank=True, default='', help_text="CC added to replies in this thread (from the rule)")
    forwarded_to = models.TextField(blank=True, default='', help_text="Where this thread was forwarded by a rule")
    ai_action = models.CharField(max_length=16, blank=True, default='',
        help_text="The AI's last decision for this thread: reply / review / escalate / ignore.")
    escalation_reason = models.TextField(blank=True, default='',
        help_text="Why the AI did not auto-reply (escalated / needs review).")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    replied_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Receptionist Lead'
        verbose_name_plural = 'Receptionist Leads'

    def __str__(self):
        return f"{self.sender_name} ({self.company_name or 'Lead'}) — {self.status}"


class ReceptionistEmailMessage(models.Model):
    """
    Multi-turn conversation history for lead threads.
    """
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', db_index=True, help_text='Owning organization (org scoping, Phase 0).',
    )
    DIRECTION_CHOICES = [
        ('inbound', 'Inbound (From Customer)'),
        ('outbound', 'Outbound (From AI Receptionist)'),
    ]

    lead = models.ForeignKey(
        ReceptionistLead,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    direction = models.CharField(max_length=16, choices=DIRECTION_CHOICES, default='inbound')
    sender = models.CharField(max_length=255)
    recipient = models.CharField(max_length=255)
    subject = models.CharField(max_length=512)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True)
    sent_at = models.DateTimeField(default=timezone.now)

    # RFC 5322 threading headers
    message_id = models.CharField(max_length=512, blank=True, default='', db_index=True)
    in_reply_to = models.CharField(max_length=512, blank=True, default='')
    references = models.TextField(blank=True, default='')
    provider_message_id = models.CharField(max_length=512, blank=True, default='',
        help_text="Provider-specific id (e.g. Microsoft Graph message id) used to reply in-thread.")

    class Meta:
        ordering = ['sent_at']
        verbose_name = 'Email Message'
        verbose_name_plural = 'Email Messages'

    def __str__(self):
        return f"[{self.direction.upper()}] {self.sender} -> {self.recipient}: {self.subject[:40]}"


class LogisticsLaneRate(models.Model):
    """
    Lane Rate Card: Origin to Destination freight matrix with vehicle specs,
    transit time, base rate, and commercial detention terms.
    """
    tenant = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='logistics_rates',
        null=True, blank=True
    )
    origin_city = models.CharField(max_length=128, db_index=True, help_text="e.g. Mumbai, Hubballi, Delhi")
    destination_city = models.CharField(max_length=128, db_index=True, help_text="e.g. Pune, Bangalore, Ahmedabad")
    vehicle_type = models.CharField(
        max_length=64,
        default='32ft Multi-Axle (15T)',
        help_text="e.g. 32ft MXL (15 Ton), 20ft Container, 14ft Canter, 40ft High Cube"
    )
    rate_type = models.CharField(
        max_length=16,
        choices=[('flat', 'Flat Trip Rate'), ('per_ton', 'Per Ton Rate'), ('per_km', 'Per KM Rate')],
        default='flat'
    )
    base_rate = models.DecimalField(max_digits=12, decimal_places=2, help_text="Rate in INR (excl. GST)")
    transit_days = models.PositiveIntegerField(default=1, help_text="Standard transit duration in days or hours")
    transit_time_label = models.CharField(max_length=64, default='24-36 Hours', help_text="Human-readable transit duration")
    detention_charge_per_day = models.DecimalField(
        max_digits=10, decimal_places=2, default=1500.00,
        help_text="Detention fee per day after standard 24 hr loading/unloading"
    )
    terms_notes = models.CharField(
        max_length=512, blank=True,
        default="Toll & Octroi included. GST 5% extra under RCM. Loading/Unloading by consignor/consignee.",
        help_text="Commercial terms to include in AI generated freight quote."
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['origin_city', 'destination_city']
        verbose_name = 'Logistics Lane Rate'
        verbose_name_plural = 'Logistics Lane Rates'

    def __str__(self):
        return f"{self.origin_city} ➔ {self.destination_city} ({self.vehicle_type}): ₹{self.base_rate}"


class LogisticsShipment(models.Model):
    """
    Shipment & Consignment Tracking registry for live LR / AWB queries and
    Vehicle Placement / Driver Dispatch.
    """
    STATUS_CHOICES = [
        ('booked', 'Booked / Order Received'),
        ('placed', 'Vehicle Placed at Loading Point'),
        ('loaded', 'Cargo Loaded & Dispatched'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered (POD Pending)'),
        ('closed', 'Completed & POD Received'),
        ('delayed', 'Delayed / Exception'),
    ]

    tenant = models.ForeignKey(
        'accounts.Organization',
        on_delete=models.CASCADE,
        related_name='logistics_shipments',
        null=True, blank=True
    )
    lr_number = models.CharField(max_length=64, unique=True, db_index=True, help_text="Lorry Receipt (LR) or AWB Number")
    customer_name = models.CharField(max_length=255, blank=True, default='')
    customer_email = models.EmailField(blank=True, null=True, db_index=True)
    origin_city = models.CharField(max_length=128)
    destination_city = models.CharField(max_length=128)
    cargo_type = models.CharField(max_length=255, default='General Cargo')
    weight_or_volume = models.CharField(max_length=128, default='15 Tons')
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='in_transit')
    current_location = models.CharField(max_length=255, blank=True, default='In Transit', help_text="Live GPS landmark or city checkpoint")
    expected_delivery = models.CharField(max_length=128, blank=True, default='', help_text="e.g. Tomorrow by 11:00 AM")
    
    # Feature 4: Vehicle Placement & Driver Details
    truck_number = models.CharField(max_length=64, blank=True, default='', help_text="e.g. MH-12-RN-4821")
    vehicle_type = models.CharField(max_length=64, blank=True, default='32ft Multi-Axle')
    driver_name = models.CharField(max_length=128, blank=True, default='', help_text="e.g. Ramesh Kumar")
    driver_phone = models.CharField(max_length=64, blank=True, default='', help_text="e.g. +91 9876543210")
    eway_bill_number = models.CharField(max_length=64, blank=True, default='')
    placement_reporting_time = models.CharField(max_length=128, blank=True, default='', help_text="Reporting time at pickup dock")

    remarks = models.TextField(blank=True, default='', help_text="Dispatch or delay notes")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Logistics Shipment'
        verbose_name_plural = 'Logistics Shipments'

    def __str__(self):
        return f"LR #{self.lr_number} ({self.origin_city} ➔ {self.destination_city}): {self.get_status_display()}"
