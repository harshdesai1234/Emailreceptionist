from django.contrib import admin
from email_receptionist.models import (
    ReceptionistConfig, ReceptionistLead, ReceptionistEmailMessage,
    LogisticsLaneRate, LogisticsShipment
)


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


@admin.register(LogisticsLaneRate)
class LogisticsLaneRateAdmin(admin.ModelAdmin):
    list_display = ('origin_city', 'destination_city', 'vehicle_type', 'base_rate', 'transit_time_label', 'is_active')
    search_fields = ('origin_city', 'destination_city', 'vehicle_type')
    list_filter = ('is_active', 'rate_type')


@admin.register(LogisticsShipment)
class LogisticsShipmentAdmin(admin.ModelAdmin):
    list_display = ('lr_number', 'origin_city', 'destination_city', 'status', 'truck_number', 'driver_name', 'driver_phone', 'current_location')
    search_fields = ('lr_number', 'truck_number', 'driver_name', 'driver_phone', 'customer_name', 'customer_email')
    list_filter = ('status', 'origin_city', 'destination_city')


@admin.register(ReceptionistEmailMessage)
class ReceptionistEmailMessageAdmin(admin.ModelAdmin):
    list_display = ('lead', 'direction', 'sender', 'recipient', 'subject', 'sent_at')
    search_fields = ('sender', 'recipient', 'subject', 'message_id')
    list_filter = ('direction', 'sent_at')
