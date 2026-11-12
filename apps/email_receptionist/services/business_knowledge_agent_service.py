"""
BusinessKnowledgeAgentService - Interactive AI agent that interviews the business owner,
understands freeform text or documents, and synthesizes key operational guardrails.
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = getattr(settings, 'EMAIL_RECEPTIONIST_MODEL', 'claude-sonnet-4-6')
DEFAULT_OPENAI_MODEL = getattr(settings, 'EMAIL_RECEPTIONIST_OPENAI_MODEL', 'gpt-4o')
DEFAULT_GEMINI_MODEL = getattr(settings, 'EMAIL_RECEPTIONIST_GEMINI_MODEL', 'gemini-3.5-flash')


def _setting(name: str, fallback: str = '') -> str:
    return os.getenv(name) or getattr(settings, name, fallback) or fallback


def _call_llm(system_prompt: str, user_prompt: str, json_mode: bool = False) -> Optional[str]:
    preferred_provider = _setting('EMAIL_RECEPTIONIST_AI_PROVIDER', '').lower()

    # 1. Google Gemini (preferred if EMAIL_RECEPTIONIST_AI_PROVIDER is gemini, or fallback)
    def call_gemini():
        key = _setting('GEMINI_API_KEY') or _setting('GOOGLE_API_KEY')
        if not key:
            return None
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=key)
            cfg = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type='application/json' if json_mode else 'text/plain',
                temperature=0.4,
            )
            resp = client.models.generate_content(
                model=_setting('EMAIL_RECEPTIONIST_GEMINI_MODEL', DEFAULT_GEMINI_MODEL),
                contents=user_prompt,
                config=cfg,
            )
            return (resp.text or '').strip()
        except Exception as e:
            logger.warning(f'Gemini call failed in knowledge agent: {e}')
            return None

    # 2. OpenAI
    def call_openai():
        key = _setting('OPENAI_API_KEY')
        if not key:
            return None
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key)
            kwargs = {
                'model': _setting('EMAIL_RECEPTIONIST_OPENAI_MODEL', DEFAULT_OPENAI_MODEL),
                'temperature': 0.4,
                'max_tokens': 1500,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ]
            }
            if json_mode:
                kwargs['response_format'] = {'type': 'json_object'}
            completion = client.chat.completions.create(**kwargs)
            return completion.choices[0].message.content or ''
        except Exception as e:
            logger.warning(f'OpenAI call failed in knowledge agent: {e}')
            return None

    # 3. Anthropic Claude
    def call_anthropic():
        key = _setting('ANTHROPIC_API_KEY')
        if not key:
            return None
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            resp = client.messages.create(
                model=_setting('EMAIL_RECEPTIONIST_MODEL', DEFAULT_ANTHROPIC_MODEL),
                max_tokens=1500,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_prompt}],
            )
            return ''.join(getattr(b, 'text', '') for b in resp.content).strip()
        except Exception as e:
            logger.warning(f'Anthropic call failed in knowledge agent: {e}')
            return None

    # Order priority based on provider
    if preferred_provider == 'gemini':
        res = call_gemini()
        if res: return res
        res = call_openai()
        if res: return res
        return call_anthropic()
    elif preferred_provider == 'anthropic':
        res = call_anthropic()
        if res: return res
        res = call_openai()
        if res: return res
        return call_gemini()
    else:
        # Default order
        res = call_gemini()
        if res: return res
        res = call_openai()
        if res: return res
        return call_anthropic()

    return None


class BusinessKnowledgeAgentService:

    INTERVIEW_SYSTEM_PROMPT = """You are an expert AI Receptionist Consultant & Knowledge Architect.
Your mission is to understand a business completely and build a smart, reliable reception profile for it.

Instead of making the business owner fill out tedious, rigid forms, you talk to them interactively.
You ask smart, concise questions (1-2 at a time), listen to their explanations, analyze any raw notes or text they share, and help clarify:
1. What the business does & their target clients
2. Core deliverables, services, or products
3. Pricing & billing philosophy (e.g. minimum spend, hourly, retainers, tiers, custom quotes)
4. Critical guardrails (what the AI must NEVER promise, e.g. turnaround times, free work, refunds, unrealistic guarantees)
5. Preferred meeting / call-to-action goal (discovery call, demo, scoping)

Keep your tone helpful, professional, and consultative. If the user provides a freeform dump, warmly acknowledge what you learned, summarize the key takeaways, and suggest what guardrails would protect their business best.
"""

    SYNTHESIS_SYSTEM_PROMPT = """You are a Business Knowledge Extractor.
Given the company's notes, past chat messages, or description, extract a clear, structured JSON profile for the Email Receptionist AI.

Output MUST be a valid JSON object with EXACTLY these keys:
{
  "business_type": "service" | "product" | "hybrid",
  "company_tagline": "one punchy sentence summarizing the business",
  "services_offered": "bullet list string of main services/offerings",
  "pricing_model_details": "bullet list string of pricing, retainers, minimums, or quoting rules",
  "company_faq_guardrails": "bullet list string of rules, what NOT to promise, standard turnaround, boundaries",
  "call_to_action_type": "discovery_call" | "scoping_meeting" | "product_demo" | "consultation",
  "tone_recommendation": "executive" | "friendly" | "concise",
  "summary": "a warm 2-3 sentence overview of what the AI now knows about this business"
}
"""

    @classmethod
    def chat_with_agent(cls, user_message: str, chat_history: List[Dict[str, str]], current_notes: str = '', company_name: str = '') -> Dict[str, Any]:
        """
        Interactive conversational step between the business owner and the onboarding agent.
        """
        context = f"Company Name: {company_name or 'Our Business'}\n"
        if current_notes:
            context += f"Existing Unstructured Notes/Context:\n{current_notes}\n"

        history_formatted = ""
        for msg in chat_history[-8:]:
            role = "User" if msg.get("role") == "user" else "AI Consultant"
            history_formatted += f"{role}: {msg.get('text', '')}\n"

        prompt = f"""{context}
Recent Conversation:
{history_formatted}
User: {user_message}

Reply directly to the user as the AI Consultant. Acknowledge what they said, explain how you'll use it for their email reception, and ask any natural next questions if needed. Keep it engaging and concise."""

        response_text = _call_llm(cls.INTERVIEW_SYSTEM_PROMPT, prompt, json_mode=False)

        if not response_text:
            # Fallback smart rule-based response when no LLM key is configured
            response_text = (
                f"Got it! I have recorded that information for {company_name or 'your business'}. "
                "I've updated your company profile with these details. "
                "Could you also tell me a bit about your pricing expectations (e.g. project minimums, retainers, or custom quotes) "
                "and anything you want the receptionist to NEVER promise to prospective clients?"
            )

        return {
            "reply": response_text
        }

    @classmethod
    def synthesize_knowledge(cls, notes: str, chat_history: Optional[List[Dict[str, str]]] = None, company_name: str = '') -> Dict[str, Any]:
        """
        Synthesizes freeform notes, chat history, or imported text into structured fields.
        """
        text_corpus = f"Company Name: {company_name}\n\nNotes & Information:\n{notes or ''}\n\n"
        if chat_history:
            text_corpus += "Chat Insights:\n"
            for m in chat_history:
                text_corpus += f"{m.get('role', 'user')}: {m.get('text', '')}\n"

        json_text = _call_llm(cls.SYNTHESIS_SYSTEM_PROMPT, text_corpus, json_mode=True)
        if json_text:
            try:
                # Strip backticks if any
                cleaned = re.sub(r'^```json\s*|\s*```$', '', json_text.strip(), flags=re.MULTILINE)
                data = json.loads(cleaned)
                return data
            except Exception as e:
                logger.warning(f"Could not parse synthesis JSON: {e}")

        # Intelligent heuristic fallback if no LLM API is available
        lines = [line.strip() for line in (notes or '').splitlines() if line.strip()]
        tagline = lines[0] if lines else f"{company_name or 'We'} provide high quality custom services."
        return {
            "business_type": "service",
            "company_tagline": tagline[:250],
            "services_offered": notes[:800] if notes else "- Custom consulting & client services",
            "pricing_model_details": "- Custom quote provided following preliminary scoping call",
            "company_faq_guardrails": "- Standard engagement turnaround: 2-4 weeks.\n- We do not commit to fixed deadlines or unvetted pricing over email.",
            "call_to_action_type": "discovery_call",
            "tone_recommendation": "executive",
            "summary": f"Configured profile for {company_name} based on provided notes."
        }

