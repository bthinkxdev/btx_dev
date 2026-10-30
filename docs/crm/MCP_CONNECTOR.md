# Claude connector (MCP)

Claude can read and change business records as the **Business Manager** role (no delete, approve or
settings). It is served by the main website at `https://<your-domain>/mcp`: no extra service and
no web-server change.

## Deploy (the whole procedure)

```bash
cd /opt/btx_dev && git pull
python manage.py migrate
# add one line to .env:
#   MCP_PUBLIC_BASE_URL=https://dev.bthinkx.com
# restart the app (same way as any other release)
```

Only if the check below answers `503 mcp_not_installed`, run `pip install -r requirements.txt` once and restart.

## Check

```bash
curl -s https://dev.bthinkx.com/.well-known/oauth-protected-resource/mcp      # JSON, not a 404
curl -si -X POST https://dev.bthinkx.com/mcp | head -1                        # HTTP 401
```

Then in claude.ai: Settings -> Connectors -> add `https://dev.bthinkx.com/mcp` -> **Connect**.

## How "Connect" works

1. claude.ai finds the sign-in endpoints and registers itself.
2. The browser opens the CRM page `/crm/mcp/authorize/<id>/`. You sign in with your normal CRM login
   (password + 2FA).
3. Only a user with the **Owner** role (or a superuser) can approve; anyone else gets a 403.
4. Approve -> claude.ai gets a one-time code and swaps it for an access token (1 hour) and a refresh
   token (30 days, replaced on every use). Tokens are stored hashed.

Every token acts as the `business_manager_ai` service account, so Claude can do exactly what that
role allows. Static keys from `manage.py mcp_issue_key` still work for other clients.

## Revoking

- `python manage.py mcp_issue_key --revoke-oauth` cuts off **all** connected apps (they must sign in again).
- `python manage.py mcp_issue_key --list` shows keys and connected apps; `--revoke <id>` kills one key.

## Notes

- The site root paths `/mcp`, `/authorize`, `/token`, `/register`, `/revoke` and `/.well-known/oauth-*`
  are fixed by the MCP/OAuth standards; they are not under `/crm/`.
- `/register` is limited to 30 requests per hour per IP.
- `manage.py run_mcp_http` (a separate process) still exists but is no longer needed.
- Wrong or missing `MCP_PUBLIC_BASE_URL` is the usual cause of a failed Connect: the sign-in links
  would point at `127.0.0.1`.
