# SMTP Relay for Office 365

A lightweight SMTP relay written in Python that accepts mail from **legacy services** (old SQL Reporting Services, ERP mailers, line-of-business apps) that cannot use modern authentication, and re-delivers every message to a single Office 365 mailbox via one of two delivery modes:

| Mode | When to use |
|---|---|
| `smtp_auth` | SMTP AUTH + STARTTLS — user account with password or App Password |
| `oauth2_graph` | Microsoft Graph API — Entra app registration with client secret (recommended when SMTP AUTH / App Passwords are unavailable) |

```
Legacy service  ──SMTP──►  smtp-relay (this server)  ──STARTTLS/AUTH──►  smtp.office365.com  ──►  destination@domain.com
(no modern auth)             listens on :25               [smtp_auth mode]        port 587
e.g. 1@domain.local
     2@domain.local                                  OR

                                                     ──Graph API HTTPS──►  graph.microsoft.com  ──►  destination@domain.com
                                                        [oauth2_graph mode]
```

---

## Prerequisites

### Option A — `smtp_auth` mode (SMTP AUTH + STARTTLS)

1. **Enable SMTP AUTH** for the O365 sending account.
   - Microsoft 365 admin center → **Users** → **Active users** → select account → **Mail** tab → **Manage email apps** → tick **Authenticated SMTP**.
   - If the tenant-wide policy is also off, a Global Admin must run:
     ```powershell
     Set-TransportConfig -SmtpClientAuthenticationDisabled $false
     ```
2. **Get a password** for the sending account.

| Scenario | What to use |
|---|---|
| No MFA | Regular account password |
| Per-user MFA with App Passwords | App Password from [My Security Info](https://mysignins.microsoft.com/security-info) |
| Conditional Access / Security Defaults | App Passwords are typically unavailable — use Option B |

### Option B — `oauth2_graph` mode (Azure app registration)

1. Go to the **Microsoft Entra admin center** → **App registrations** → **New registration**.
   - Name: e.g. `smtp-relay` · Redirect URI: leave empty · Register.
2. Note the **Application (client) ID** and **Directory (tenant) ID** from the overview.
3. **Certificates & secrets** → **New client secret** → give it a description and expiry → **Add**.
   - Copy the **Value** (shown only once). It is a long string with letters, digits and special characters — not a fixed length or format.
4. **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions** → `Mail.Send` → **Add** → **Grant admin consent**.

---

## Quick start — Docker (recommended)

```bash
# 1. Clone / copy the project
cd smtp2o365

# 2. Create the config file
copy .env.example .env
# Edit .env — set AUTH_MODE, O365_USERNAME, and either the password (smtp_auth)
# or the three Azure variables (oauth2_graph)

# 3. Start the relay
docker compose up -d

# 4. Watch the logs
docker compose logs -f
```

The relay will listen on **port 25** of the host machine.

---

## Quick start — Python (no Docker)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create the config file
copy .env.example .env
# Edit .env

# 4. Run (on Windows, run the terminal as Administrator for port 25,
#         OR change LISTEN_PORT=2525 in .env to avoid that requirement)
python relay.py
```

---

## Configuration reference

All settings are in `.env` (copy from `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | IP to bind the relay on |
| `LISTEN_PORT` | `25` | TCP port to listen on (`2525` if 25 is unavailable) |
| `AUTH_MODE` | `smtp_auth` | `smtp_auth` or `oauth2_graph` — see above |
| `O365_USERNAME` | — | **Required (both modes).** Mailbox that sends the mail |
| `O365_PASSWORD` | — | **smtp_auth only.** Account password or App Password |
| `O365_SMTP_HOST` | `smtp.office365.com` | O365 SMTP endpoint *(smtp_auth only)* |
| `O365_SMTP_PORT` | `587` | O365 SMTP port *(smtp_auth only)* |
| `AZURE_TENANT_ID` | — | **oauth2_graph only.** Directory (tenant) ID |
| `AZURE_CLIENT_ID` | — | **oauth2_graph only.** Application (client) ID |
| `AZURE_CLIENT_SECRET` | — | **oauth2_graph only.** Client secret value |
| `FORWARD_TO` | `test@domain.com` | Final delivery address for all relayed messages |
| `REWRITE_FROM` | `true` | Replace `From` header with `O365_USERNAME` (recommended) |
| `ALLOWED_SENDERS` | `1@domain.local,2@domain.local` | Exact sender addresses permitted |
| `ALLOWED_SENDER_DOMAINS` | `domain.local` | Whole domains permitted (any `@domain`) |
| `ALLOWED_CLIENT_IPS` | `127.0.0.1,::1` | Client IPs allowed to connect. **Add your server IPs here.** Leave empty to allow all (isolated networks only). |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Configuring your legacy services

Point each legacy application's **SMTP server / smarthost** setting to the machine running this relay:

| Setting | Value |
|---|---|
| SMTP Server | `<IP of this machine>` |
| Port | `25` (or `2525` if you changed it) |
| Authentication | **None** (the relay handles O365 auth internally) |
| TLS/SSL | **None** / disabled (plain SMTP to the relay) |

> **Security note:** The relay intentionally accepts unauthenticated connections from legacy clients. It is protected by the `ALLOWED_CLIENT_IPS` and `ALLOWED_SENDERS` allow-lists. **Never expose port 25 to the internet** — bind it to an internal interface or restrict it with a firewall rule.

---

## What happens to each message

1. Legacy service sends an email with `From: 1@domain.local` (or `2@domain.local`).
2. The relay accepts the connection (IP + sender checks pass).
3. Before forwarding, the relay:
   - Adds `X-Original-From: 1@domain.local` and `X-Original-To: <whatever>` headers.
   - **Rewrites `From`** to `test@domain.com` (O365_USERNAME) so O365 accepts the submission.
   - Sets `Reply-To: 1@domain.local` so replies go back to the original sender.
4. The relay connects to `smtp.office365.com:587`, authenticates, and delivers to `test@domain.com`.
5. The user at `test@domain.com` receives the email and can see the original sender in `Reply-To` / `X-Original-From`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `451 4.7.0 Upstream authentication failure` | Wrong credentials or SMTP AUTH not enabled | Check `O365_USERNAME`/`O365_PASSWORD`, enable SMTP AUTH on the account |
| `451 4.3.0 … OAuth2 token acquisition failed` | Wrong tenant/client ID or secret, or missing admin consent | Verify `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`; grant admin consent on Mail.Send |
| `451 4.3.0 … Graph API /sendMail returned 403` | App lacks Mail.Send permission or admin consent not granted | Grant **Mail.Send** application permission and click **Grant admin consent** in Entra |
| `550 5.7.1 Client not authorized` | Legacy server IP not in allow-list | Add the server IP to `ALLOWED_CLIENT_IPS` in `.env` |
| `550 5.7.1 Sender not allowed` | Sender address/domain not in allow-list | Add it to `ALLOWED_SENDERS` or `ALLOWED_SENDER_DOMAINS` |
| Connection refused on port 25 | Port in use or permission denied | Change `LISTEN_PORT=2525` in `.env`; on Windows run as Administrator for port 25 |
| `535 5.7.139 … SmtpClientAuthentication is disabled` | Tenant-wide SMTP AUTH is off | Enable it in the Exchange admin center or via PowerShell (see Prerequisites), or switch to `AUTH_MODE=oauth2_graph` |

Enable `LOG_LEVEL=DEBUG` for detailed SMTP conversation logs when diagnosing issues.

---

## Security considerations

- The relay is **not an open relay**: it rejects connections from IPs not in `ALLOWED_CLIENT_IPS` and mail from senders not matching `ALLOWED_SENDERS` / `ALLOWED_SENDER_DOMAINS`.
- O365 credentials are **never logged** regardless of log level.
- All traffic to O365 is encrypted with **TLS via STARTTLS**.
- Connections from legacy services to this relay are **plain SMTP** (internal network only) — acceptable since they originate from the same LAN/VLAN.
- Store the `.env` file securely and exclude it from version control (add `.env` to `.gitignore`).
