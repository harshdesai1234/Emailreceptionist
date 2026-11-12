"""
TemplateService — reusable email templates the user can insert into a draft.

Placeholders available in built-in and custom templates:
  {first_name}   sender's first name
  {subject}      original subject / intent
  {company} / {our_company}  the organization's name
  {booking_line} "You can book a time here: <link>" or an availability request
  {booking_url}  the raw booking link (may be empty)
  {signature}    the organization's sign-off

Inserted templates are always treated as drafts for a human to send.
"""
from typing import Any, Dict, List


class _SafeDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'


class TemplateService:

    TEMPLATES = {
        # ── RECEPTIONIST (INBOUND) ─────────────────────────────────────────
        "rec_bdr_discovery": {
            "key": "rec_bdr_discovery",
            "name": "Discovery Call Invite",
            "category": "receptionist",
            "subject": "Re: {subject}",
            "body": (
                "Hi {first_name},\n\n"
                "Thank you for reaching out to {our_company}! We'd be glad to learn more about what you need "
                "and walk you through how we can help.\n\n"
                "{booking_line}\n\n"
                "Looking forward to speaking with you.\n\n"
                "Best regards,\n{signature}"
            ),
        },
        "rec_intake_confirm": {
            "key": "rec_intake_confirm",
            "name": "Inquiry Received Confirmation",
            "category": "receptionist",
            "subject": "Re: {subject}",
            "body": (
                "Hello {first_name},\n\n"
                "Thank you for contacting {our_company}. We have received your inquiry and our team is reviewing it now. "
                "You can expect a detailed reply shortly.\n\n"
                "{booking_line}\n\n"
                "Warm regards,\n{signature}"
            ),
        },
        "rec_after_hours": {
            "key": "rec_after_hours",
            "name": "After-Hours Response",
            "category": "receptionist",
            "subject": "Re: {subject}",
            "body": (
                "Hi {first_name},\n\n"
                "Thank you for writing to {our_company}. Our team is currently outside office hours, but your message "
                "has been received and we will get back to you first thing on the next business day.\n\n"
                "{booking_line}\n\n"
                "Best regards,\n{signature}"
            ),
        },

        # ── EMAIL AGENT (FOLLOW-UP) ────────────────────────────────────────
        "agent_tech_spec": {
            "key": "agent_tech_spec",
            "name": "Technical Follow-up",
            "category": "agent",
            "subject": "Re: {subject}",
            "body": (
                "Hi {first_name},\n\n"
                "Thanks for sharing your requirements. I've passed the technical details to our team so we can give you "
                "an accurate answer.\n\n"
                "Would a short technical call help? {booking_line}\n\n"
                "Best regards,\n{signature}"
            ),
        },
        "agent_roi_pricing": {
            "key": "agent_roi_pricing",
            "name": "Pricing Follow-up",
            "category": "agent",
            "subject": "Re: {subject}",
            "body": (
                "Hello {first_name},\n\n"
                "Thank you for your interest. Pricing depends on your scope, so the quickest way to get you an accurate "
                "quote is a brief conversation about what you need.\n\n"
                "{booking_line}\n\n"
                "Best regards,\n{signature}"
            ),
        },
        "agent_objection_reengage": {
            "key": "agent_objection_reengage",
            "name": "Gentle Follow-up",
            "category": "agent",
            "subject": "Following up: {subject}",
            "body": (
                "Hi {first_name},\n\n"
                "I wanted to check back in on our earlier conversation. If you have any questions at all, I'm happy to help.\n\n"
                "{booking_line}\n\n"
                "Best regards,\n{signature}"
            ),
        },
    }

    @classmethod
    def get_templates_for_category(cls, category: str = "all", config=None) -> List[Dict[str, Any]]:
        """Returns templates filtered by category, merging tenant custom templates if provided."""
        all_tpls = dict(cls.TEMPLATES)
        if config and getattr(config, 'custom_templates', None):
            for ct in config.custom_templates:
                if isinstance(ct, dict) and ct.get('key'):
                    all_tpls[ct['key']] = ct
        if category == "all":
            return list(all_tpls.values())
        return [t for t in all_tpls.values() if t.get("category") == category]

    @classmethod
    def format_template(cls, template_key: str, first_name: str = "there", subject: str = "your inquiry",
                        booking_url: str = "", config=None, **_ignored) -> Dict[str, str]:
        """Formats a built-in or custom template with lead + organization variables."""
        from email_receptionist.services.ai_draft_service import _company_name, _signature, _booking_line

        tpl = cls.TEMPLATES.get(template_key)
        if config and getattr(config, 'custom_templates', None):
            for ct in config.custom_templates:
                if isinstance(ct, dict) and ct.get('key') == template_key:
                    tpl = ct
                    break
        if not tpl:
            return {"subject": subject, "body": ""}

        booking_url = booking_url or (getattr(config, 'booking_calendar_url', '') if config else '') or ''
        booking_line = _booking_line(config)
        fmt_vars = _SafeDict(
            first_name=first_name or "there",
            subject=subject or "your inquiry",
            our_company=_company_name(config),
            company=_company_name(config),  # the settings screen documents {company}
            booking_line=booking_line,
            booking_url=booking_url,
            signature=_signature(config),
            # legacy placeholders used by older custom templates
            company_name=_company_name(config),
            selected_slot="a time that suits you",
        )
        try:
            formatted_subject = str(tpl.get("subject", subject)).format_map(fmt_vars)
            formatted_body = str(tpl.get("body", "")).format_map(fmt_vars)
        except Exception:
            formatted_subject = tpl.get("subject", subject)
            formatted_body = tpl.get("body", "")
        return {"key": template_key, "subject": formatted_subject, "body": formatted_body}
