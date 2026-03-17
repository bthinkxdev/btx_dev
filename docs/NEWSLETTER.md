# Blog newsletter

## Subscribe

- Blog footer form POSTs to `/newsletter/subscribe/` and stores emails in `NewsletterSubscriber`.
- Unsubscribe: link in every notification email → `/newsletter/unsubscribe/<token>/`.

## New post emails

When a post is **first published** (new published post, or draft → published), a **daemon thread** sends one email per active subscriber, with **`NEWSLETTER_EMAIL_INTERVAL_SECONDS`** delay between messages (default `2.0`) to reduce SMTP rate-limit issues.

## Environment (production)

| Variable | Purpose |
|----------|---------|
| `SITE_BASE_URL` | Canonical site URL, e.g. `https://yoursite.com` (used in article + unsubscribe links) |
| `NEWSLETTER_EMAIL_INTERVAL_SECONDS` | Pause between each recipient (e.g. `2.5`) |
| `EMAIL_*` / `DEFAULT_FROM_EMAIL` | Same as contact form |

## Reliability

Threads are fine for low volume. For hundreds of subscribers or strict delivery guarantees, move the logic in `pages/newsletter_tasks.py` into **Celery / RQ / Dramatiq** (same loop + sleep or queue per recipient).

## Resend notifications

Admin → Blog posts → select posts → **Reset newsletter state** → unpublish → publish again (or save as published after reset).
