from django.contrib import admin
from email_receptionist.models import ReceptionistConfig, ReceptionistLead, ReceptionistEmailMessage


@admin.register(ReceptionistConfig)
class ReceptionistConfigAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'email_provider', 'mailbox_connected_email', 'is_autonomous_mode', 'default_tone', 'updated_at')
    search_fields = ('tenant__name', 'mailbox_connected_email')
    list_filter = ('email_provider', 'is_autonomous_mode', 'default_tone')


@admin.register(ReceptionistLead)
class ReceptionistLeadAdmin(admin.ModelAdmin):
    list_display = ('sender_name', 'sender_email', 'company_name', 'origin_city', 'destination_city', 'cargo_type', 'load_type', 'status', 'created_at')
    search_fields = ('sender_name', 'sender_email', 'company_name', 'origin_city', 'destination_city', 'tracking_number')
    list_filter = ('status', 'load_type', 'lead_type', 'is_spam')


@admin.register(ReceptionistEmailMessage)
class ReceptionistEmailMessageAdmin(admin.ModelAdmin):
    list_display = ('lead', 'direction', 'sender', 'recipient', 'subject', 'sent_at')
    search_fields = ('sender', 'recipient', 'subject', 'message_id')
    list_filter = ('direction', 'sent_at')
