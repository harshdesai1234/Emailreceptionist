from django.urls import path
from email_receptionist import views

app_name = 'email_receptionist'

urlpatterns = [
    # Dashboard Primary View
    path('', views.dashboard, name='dashboard'),

    # Core Interactive API Endpoints
    path('api/generate-draft/', views.api_generate_draft, name='api_generate_draft'),
    path('api/send-reply/', views.api_send_reply, name='api_send_reply'),
    path('api/cancel-auto-send/', views.api_cancel_auto_send, name='api_cancel_auto_send'),
    path('api/escalate/', views.api_escalate_lead, name='api_escalate_lead'),
    path('api/archive/', views.api_archive_lead, name='api_archive_lead'),
    path('api/save-config/', views.api_save_config, name='api_save_config'),
    path('api/knowledge-chat/', views.api_knowledge_agent_chat, name='api_knowledge_agent_chat'),
    path('api/synthesize-knowledge/', views.api_synthesize_knowledge, name='api_synthesize_knowledge'),
    path('api/upload-knowledge-doc/', views.api_upload_knowledge_document, name='api_upload_knowledge_document'),
    path('api/scrape-knowledge-site/', views.api_scrape_knowledge_website, name='api_scrape_knowledge_website'),
    path('api/clear-knowledge-chat/', views.api_clear_knowledge_chat, name='api_clear_knowledge_chat'),
    path('api/webhook/', views.api_inbound_webhook, name='api_inbound_webhook'),
    path('api/regenerate-webhook-token/', views.api_regenerate_webhook_token, name='api_regenerate_webhook_token'),

    # Mailbox Management (cost-free, uses imaplib)
    path('api/poll-mailbox/', views.api_poll_mailbox, name='api_poll_mailbox'),
    path('api/disconnect-mailbox/', views.api_disconnect_mailbox, name='api_disconnect_mailbox'),
    path('api/test-connection/', views.api_test_mailbox_connection, name='api_test_mailbox_connection'),
    path('api/send-test-email/', views.api_send_test_email, name='api_send_test_email'),
    path('api/preview-reply/', views.api_preview_reply, name='api_preview_reply'),

    # Lead Thread History
    path('api/lead-messages/', views.api_get_lead_messages, name='api_get_lead_messages'),

    # Logistics Intelligence (Lane Rates, Shipments, Bulk Upload & TMS Webhook)
    path('api/logistics/rates/', views.api_list_lane_rates, name='api_list_lane_rates'),
    path('api/logistics/rates/save/', views.api_save_lane_rate, name='api_save_lane_rate'),
    path('api/logistics/shipments/', views.api_list_shipments, name='api_list_shipments'),
    path('api/logistics/shipments/save/', views.api_save_shipment, name='api_save_shipment'),
    path('api/logistics/shipments/upload/', views.api_upload_dispatch_sheet, name='api_upload_dispatch_sheet'),
    path('api/logistics/webhook/tms-sync/', views.api_tms_shipment_webhook, name='api_tms_shipment_webhook'),

    # Google 1-Click OAuth Flow
    path('auth/google/connect/', views.google_oauth_connect, name='google_oauth_connect'),
    path('auth/google/callback/', views.google_oauth_callback, name='google_oauth_callback'),

    # Microsoft / Office 365 1-Click OAuth Flow
    path('auth/microsoft/connect/', views.microsoft_oauth_connect, name='microsoft_oauth_connect'),
    path('auth/microsoft/callback/', views.microsoft_oauth_callback, name='microsoft_oauth_callback'),

    # Zoho Mail 1-Click OAuth Flow
    path('auth/zoho/connect/', views.zoho_oauth_connect, name='zoho_oauth_connect'),
    path('auth/zoho/callback/', views.zoho_oauth_callback, name='zoho_oauth_callback'),
]

