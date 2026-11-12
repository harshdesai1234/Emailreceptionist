# Agentyne Email Receptionist (Standalone Developer Build)

This is an isolated, standalone build of the **Agentyne Email Receptionist** application. It contains all models, services, views, interactive dashboard UI, and mailbox polling commands required to run and develop the Email Receptionist without any dependencies on other proprietary platform modules.

---

## Features Included

- **AI Front-Desk & Inbound Triage:** Automatically scores, classifies, and categorizes incoming customer inquiries.
- **Autonomous & Supervised Reply Modes:** AI writes context-aware draft replies based on company guardrails, pricing, and FAQs; either sends automatically with an undo window or queues for human review.
- **Multi-Provider Mailbox Sync:** IMAP/SMTP polling with support for Google Workspace, Microsoft 365, Zoho Mail, and custom servers.
- **Rules Engine:** Topic/product-based routing, forwarding, and pitching.
- **Interactive UI Dashboard:** Lead conversation viewer, AI reply tester, manual trigger sync, mailbox connection test, and guardrails configuration.
- **Built-in Authentication & Workspace Scoping:** Includes a lightweight `accounts` module providing user authentication and organization workspace isolation.

---

## Quick Start Guide

### 1. Prerequisites
- Python 3.10 or 3.11
- pip and virtualenv

### 2. Environment Setup

```bash
# Create and activate a virtual environment
python -m venv venv

# Windows (cmd/PowerShell):
venv\Scripts\activate

# macOS / Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure `.env`

Copy the sample environment file:

```bash
# Windows:
copy .env.example .env

# macOS / Linux:
cp .env.example .env
```

Generate an encryption key for mailbox passwords and paste it into `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add at least one AI API key if you want live LLM generation:
- `ANTHROPIC_API_KEY` (Claude)
- `OPENAI_API_KEY` (GPT-4o)
- `GEMINI_API_KEY` (Google Gemini)

*(Note: If no API key is set, the app falls back to built-in template drafts for safe testing.)*

### 4. Database Setup & Demo Seed

```bash
# Run migrations
python manage.py migrate

# Seed demo user (admin / admin123), workspace organization, and sample leads
python manage.py setup_demo
```

### 5. Run Development Server

```bash
python manage.py runserver
```

Now open **`http://127.0.0.1:8000/`** in your browser:
- **Username:** `admin`
- **Password:** `admin123`

You will land directly on the **Email Receptionist** dashboard with pre-loaded sample leads and configuration!

---

## Local Mailbox Polling (Without Celery)

In production, mailbox polling runs on Celery beat (`poll_all_mailboxes_task` every 2 min).  
For local development, you can poll manually or in a continuous loop:

```bash
# Poll once:
python manage.py poll_mailbox

# Continuous loop every 60 seconds:
python manage.py poll_mailbox --loop --interval 60
```

---

## Running Automated Tests

The test suite mocks all external LLM and SMTP/IMAP calls:

```bash
python manage.py test email_receptionist
```

---

## Project Structure

```
email_receptionist/
├── manage.py
├── requirements.txt
├── .env.example
├── README.md
├── config/
│   ├── settings.py           # Core Django settings (SQLite default)
│   ├── urls.py               # Root URL router
│   ├── wsgi.py
│   └── asgi.py
├── apps/
│   ├── accounts/             # Lightweight User & Organization workspace models
│   └── email_receptionist/   # The core Email Receptionist application
│       ├── models.py         # ReceptionistConfig, ReceptionistLead, ReceptionistEmailMessage
│       ├── views.py          # Dashboard view & interactive AJAX APIs
│       ├── urls.py           # Application endpoints
│       ├── services/         # AI drafting, IMAP poller, triage, rules, auto-reply
│       ├── management/       # poll_mailbox command
│       └── templates/        # Dashboard HTML
├── templates/
│   ├── dashboard/base.html   # Main layout shell
│   ├── components/           # Sidebar, topbar, alerts
│   └── accounts/             # Login & profile pages
└── static/                   # Bootstrap, FontAwesome, and Agentyne styling
```
