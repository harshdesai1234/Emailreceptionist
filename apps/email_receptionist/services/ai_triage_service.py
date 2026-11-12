import re
from typing import Dict, Any


class AITriageService:
    SPAM_PATTERNS = [
        r'out of office',
        r'automatic reply',
        r'auto-reply',
        r'undelivered mail',
        r'mailer-daemon',
        r'delivery status notification',
        r'do not reply',
        r"don'?t reply",
        r'do not respond',
        r"don'?t respond",
        r'no reply needed',
        r'no-reply',
        r'noreply',
        r'please do not reply',
        r'this is an automated message',
        r'automated email - do not reply',
        r'unsubscribe',
        r'security alert',
        r'password reset',
        r'reset your password',
        r'profile photo',
        r'renderdatainput',
        r'linkedin',
        r'checkpoint-password',
        r'account_security_alert',
        r'two-step-verification',
        r'verification code',
        r'confirm your email',
        r'sign-in alert',
        r'new login',
        r'account activity',
        r'privacy policy update',
        r'terms of service',
    ]

    # Mailbox names that are (almost) never a person writing to you: receipts, bank alerts, shipping
    # updates, newsletters. Matched against the part before the @.
    AUTOMATED_SENDER_RE = re.compile(
        r'^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply[-_.].*|.*[-_.]no[-_.]?reply|mailer[-_.]daemon|postmaster|bounces?'
        r'|notifications?|notify|alerts?|updates?|news(letter)?|newsletters?|marketing|promo(tions)?|offers?|deals'
        r'|orders?|order[-_.]update|shipment|shipping|tracking|delivery|receipts?|invoices?|billing|statements?|estatement'
        r'|transactions?|txn|otp|verification|security|account[-_.]?(alerts?|update)|auto[-_.]?(confirm|reply|mailer)?|system|daemon'
        r'|digest|feedback|survey|reminders?|calendar[-_.]notification)\b',
        re.IGNORECASE,
    )

    @classmethod
    def is_automated_sender(cls, sender_email: str = "") -> bool:
        local = (sender_email or '').split('@')[0].strip().lower()
        return bool(local) and bool(cls.AUTOMATED_SENDER_RE.match(local))

    @classmethod
    def is_spam_or_autoreply(cls, subject: str, body: str, sender_email: str = "") -> bool:
        """
        Detects if an incoming email is an automated receipt, security alert, system notification, or spam.
        """
        if cls.is_automated_sender(sender_email):
            return True
        combined = f"{subject} {body} {sender_email}".lower()
        for pattern in cls.SPAM_PATTERNS:
            if re.search(pattern, combined):
                return True
        return False

    @classmethod
    def analyze_lead_intent(cls, subject: str, message: str, role: str = "", team_size: str = "") -> Dict[str, Any]:
        """
        Classifies intent, computes 0-100 qualification score, and determines urgency.
        """
        text = f"{subject} {message}".lower()
        
        # 1. Determine Intent Category
        if any(w in text for w in ['demo', 'presentation', 'schedule', 'tour', 'walkthrough', 'meeting']):
            intent = 'Enterprise Demo'
        elif any(w in text for w in ['price', 'pricing', 'cost', 'quote', 'quotation', 'rate', 'budget', 'tier']):
            intent = 'Pricing & Quotation'
        elif any(w in text for w in ['multi-tenant', 'sub-brand', 'agency', 'tenant', 'routing', 'division']):
            intent = 'Multi-Tenant Routing'
        elif any(w in text for w in ['api', 'webhook', 'integration', 'sdk', 'crm', 'hubspot', 'salesforce']):
            intent = 'Technical Integration'
        else:
            intent = 'Product Inquiry'

        # 2. Compute Qualification Score (Base 70)
        score = 70

        # High urgency keywords (+15)
        if any(w in text for w in ['urgent', 'asap', 'immediately', 'this week', 'evaluate', 'evaluating', 'decision']):
            score += 15

        # Decision maker role bonus (+10)
        r_lower = role.lower()
        if any(w in r_lower for w in ['vp', 'director', 'head', 'cto', 'ceo', 'founder', 'manager', 'executive']):
            score += 10

        # Enterprise team size bonus (+10)
        t_lower = team_size.lower()
        if any(w in t_lower for w in ['100', '120', '200', '300', '500', '1000', 'enterprise', 'large']):
            score += 10

        # Clamp between 50 and 99
        score = min(99, max(50, score))

        # 3. Urgency
        urgency = 'High' if score >= 85 else ('Medium' if score >= 75 else 'Standard')

        # 4. Generate dynamic fit reasoning
        fit_reason = cls.generate_fit_reason(
            score=score,
            intent=intent,
            role=role,
            team_size=team_size,
            message=message
        )

        return {
            "intent": intent,
            "qualification_score": score,
            "urgency": urgency,
            "is_hot_lead": score >= 85,
            "fit_reason": fit_reason
        }

    @classmethod
    def generate_fit_reason(cls, score: int, intent: str, role: str, team_size: str, message: str) -> str:
        """
        Synthesizes a realistic 1-sentence fit explanation detailing why this lead scored as they did.
        """
        msg_l = message.lower()
        reasons = []

        if role:
            reasons.append(f"Decision-maker presence ({role})")
        if any(w in msg_l for w in ['budget', '$', 'quote', 'cost', 'pricing']):
            reasons.append("budget inquiry identified")
        if any(w in msg_l for w in ['urgent', 'asap', 'this week', 'immediately', 'evaluating']):
            reasons.append("high purchasing urgency")
        if any(w in msg_l for w in ['team', 'company', 'organization', 'employees']):
            reasons.append(f"scaled organization ({team_size or 'mid-market'})")
        if any(w in msg_l for w in ['demo', 'call', 'meeting', 'walkthrough']):
            reasons.append("requesting direct consultation")

        if score >= 85:
            prefix = "High Strategic Fit:"
            detail = "; ".join(reasons) if reasons else f"Strong alignment with {intent} and executive criteria."
        elif score >= 70:
            prefix = "Qualified Evaluation:"
            detail = "; ".join(reasons) if reasons else f"Inquiry regarding {intent} requires preliminary scoping."
        else:
            prefix = "Exploratory Fit:"
            detail = "General introductory inquiry; needs qualification on budget, scope, and decision timeline."

        return f"{prefix} {detail}"

    @classmethod
    def extract_signature_details(cls, body: str, sender_name: str = "", sender_email: str = "") -> Dict[str, Any]:
        """
        Parses sender email and body to dynamically extract job title, phone number, and company.
        """
        extracted = {
            "role": None,
            "phone": None,
            "company": None
        }

        # 1. Extract Phone Number
        phone_match = re.search(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', body)
        if phone_match:
            extracted["phone"] = phone_match.group(0).strip()

        # 2. Extract Job Title / Role
        role_patterns = [
            r'(?:Vice President|VP|Director|Head|Chief|CTO|CEO|COO|CMO|CFO|Founder|Co-Founder|Lead|Architect|Engineer|Manager|Owner)\s*(?:of\s+[A-Za-z\s]+)?',
            r'(?:Product Manager|Engineering Lead|Operations Manager|Sales Director|Account Executive)',
        ]
        for pat in role_patterns:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                extracted["role"] = m.group(0).strip().title()
                break

        # 3. Extract Company from email domain or signature
        if sender_email and '@' in sender_email:
            domain = sender_email.split('@')[-1].lower()
            free_providers = ['gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in', 'hotmail.com', 'outlook.com', 'live.com',
                              'msn.com', 'icloud.com', 'me.com', 'proton.me', 'protonmail.com', 'aol.com', 'rediffmail.com', 'zoho.com', 'gmx.com']
            if domain not in free_providers:
                # e.g. "techsphere.io" -> "Techsphere"
                company_slug = domain.split('.')[0]
                extracted["company"] = company_slug.replace('-', ' ').replace('_', ' ').title()

        return extracted
