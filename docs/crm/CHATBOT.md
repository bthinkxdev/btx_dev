# Website chatbot and AI sales analyst

## What it is
- **Website assistant ("Bee")**: floating robot icon (blinking eyes, greets visitors) on every public page. Answers
  about packages, pricing, services and positioning, collects name + phone and creates a CRM lead
  (source **Website Chatbot**, round-robin assigned, managers notified). Public API under `/chat/api/`.
- **Website chats** (CRM > Leads > Website chats): full history of every chat, with the lead it created.
  Visible only to Sales Manager and Owner (module `chats`).
- **AI sales analyst** (CRM > Reports > AI sales analyst): ask questions about each sales executive's and manager's
  performance. Visible only to Sales Manager and Owner (module `ai_analyst`). Only aggregate numbers and staff names
  go to the AI, never customer details.

## Free AI setup (one-time, about 2 minutes, no payment)
1. Create a free account at https://console.groq.com and create an API key.
2. Put it in the server `.env`: `CHATBOT_API_KEY=gsk_...`
3. Restart the app. That is all.

Free-tier limits are respected: `CHATBOT_DAILY_AI_LIMIT` (default 900 AI answers/day) is a safety cap. When the key
is missing, invalid or the quota is used up, the chatbot automatically falls back to built-in answers
(package prices come from the same table as the quotation PDFs) and the analyst shows a built-in leaderboard.
To use another free provider (Gemini, OpenRouter, ...) set `CHATBOT_BASE_URL` and `CHATBOT_MODEL`.

## Deploy
```
git pull && pip install -r requirements.txt
python manage.py migrate            # creates chat tables and grants owner + sales_manager access
python manage.py collectstatic --noinput
# restart gunicorn
```
Role stacking: anyone who also holds the `sales_manager` role (e.g. an executive with both roles) can see chats.
Check with `python manage.py crm_user_access <username>`.

## Editing what the bot says
`crm/modules/chatbot/knowledge.py` (company facts, services, marketing tiers, system prompt, built-in answers).

## Abuse protection
Origin check, per-IP rate limits (start 12/h, messages 40/10min, lead form 6/h), honeypot field, message length cap,
daily AI cap, and no CSRF cookie dependency (so a stale browser cookie can never break a chat).
