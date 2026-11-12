# Email Receptionist

Reads the organization's company inbox, writes a reply with AI using the company's own
knowledge, and either sends it automatically (Autonomous mode) or waits for approval.
Lives under **Receptionist → Email** (`/ai-receptionist/email/`), next to the Voice receptionist.

## How a reply is decided
1. New email arrives (IMAP / Microsoft Graph poll every 2 min, the Sync button, or the web-form webhook).
2. Spam, auto-replies, newsletters and mailing lists are filtered (headers + keywords) and never answered.
3. The email is matched to its conversation (In-Reply-To / References, else same sender within 30 days).
4. The AI (Claude, OpenAI or Gemini) writes the reply using: Guardrails settings (company, services, pricing, FAQs, booking link, tone)
   + the last 10 messages of the thread. It returns `reply`, `review` or `escalate`.
5. Autonomous mode ON and `reply` and inside the coverage schedule and under the per-thread limit
   → scheduled; sent after the undo delay unless someone clicks **Cancel auto-send**.
   Otherwise the draft waits for a human (`escalate` also emails the organization owner).

## Setup
1. `python manage.py migrate email_receptionist`
2. `.env`:
   - At least one AI key (already used elsewhere in the project):
     `ANTHROPIC_API_KEY` (Claude), `OPENAI_API_KEY` (OpenAI), `GOOGLE_API_KEY` or `GEMINI_API_KEY` (Gemini).
     Order: Claude → OpenAI → Gemini; the next one is used if one fails. No key = basic draft, never auto-sent.
   - optional `EMAIL_RECEPTIONIST_AI_PROVIDER` = `auto` | `claude` | `openai` | `gemini` (which to try first)
   - optional models: `EMAIL_RECEPTIONIST_MODEL` (Claude, default `claude-sonnet-4-6`),
     `EMAIL_RECEPTIONIST_OPENAI_MODEL` (default `gpt-4o`), `EMAIL_RECEPTIONIST_GEMINI_MODEL` (default `gemini-3.5-flash`)
   - `EMAIL_RECEPTIONIST_ENCRYPTION_KEY` – generate once:
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
     (never change it afterwards, or saved mailbox passwords can't be read)
   - for 1-click sign-in: `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `MICROSOFT_OAUTH_CLIENT_ID/SECRET`
3. Celery worker + beat must run (tasks: `poll_all_mailboxes_task` every 2 min,
   `dispatch_due_replies_task` every 30 s). Without Celery: `python manage.py poll_mailbox --loop`.
4. In the dashboard: **Mailbox** → connect; **Guardrails** → fill company knowledge; then choose
   Supervised or Autonomous.

## Testing it
- **Test AI** (dashboard header): type a sample customer email and see the exact reply, and whether it would be
  sent, reviewed or escalated. Nothing is sent or saved.
- **Send Test Email** (Mailbox window): sends one email from the saved mailbox to an address you choose.
- **Test Connection** (Mailbox window): checks the mailbox login only.
- End-to-end: email the connected inbox from a personal address, click **Sync**, keep Supervised mode on.

## Notes
- Gmail via OAuth uses a Google *restricted* scope: Google app verification (incl. a security
  assessment) is needed before external customers can use it. Gmail with an App Password works without it.
- Zoho: use Zoho email + App Password (IMAP/SMTP). Zoho 1-click only links the account.
- Webhook: `POST /ai-receptionist/email/api/webhook/` with header `X-Webhook-Token` (shown in Guardrails).
- Tests: `python manage.py test email_receptionist`
