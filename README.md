# SMTP Relay for Office 365

A lightweight SMTP relay written in Python that accepts mail from **legacy services** (old SQL Reporting Services, ERP mailers, line-of-business apps) that cannot use modern authentication, and re-delivers every message to a single Office 365 mailbox via **SMTP AUTH with STARTTLS**.

```
Legacy service  ──SMTP──►  smtp-relay (this server)  ──STARTTLS/AUTH──►  smtp.office365.com  ──►  test@some.es
(no modern auth)             listens on :25                port 587
e.g. 1@some.local
     2@some.local
```

---

## Prerequisites

### 1 — Enable SMTP AUTH for the O365 sending account

Microsoft disables per-account SMTP AUTH by default. You must enable it before the relay can authenticate.

1. Sign in to the **Microsoft 365 admin center** → **Users** → **Active users**.
2. Select the account that will send mail (e.g. `test@some.es`).
3. Open the **Mail** tab → **Manage email apps**.
4. Tick **Authenticated SMTP** and save.

> If the tenant-wide SMTP AUTH policy is also disabled, a Global Admin may need to run:
> ```powershell
> Set-TransportConfig -SmtpClientAuthenticationDisabled $false
> ```

### 2 — Get a password for the sending account

| Scenario | What to use |
|---|---|
| Account has no MFA | Use the regular account password |
| Account has MFA / per-user MFA | Create an **App Password** (My Account → Security info → App passwords) |
| Tenant uses Security Defaults or Conditional Access | Either exclude the service account from CA, or use a dedicated shared mailbox with a known password |

---

## Quick start — Docker (recommended)

```bash
# 1. Clone / copy the project
cd MailServerSOME

# 2. Create the config file
copy .env.example .env
# Edit .env and fill in O365_USERNAME, O365_PASSWORD, ALLOWED_CLIENT_IPS, etc.

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
| `O365_SMTP_HOST` | `smtp.office365.com` | O365 SMTP endpoint |
| `O365_SMTP_PORT` | `587` | O365 SMTP port (STARTTLS) |
| `O365_USERNAME` | — | **Required.** O365 account that authenticates and sends |
| `O365_PASSWORD` | — | **Required.** Account or app password |
| `FORWARD_TO` | `test@some.es` | Final delivery address for all relayed messages |
| `REWRITE_FROM` | `true` | Replace `From` header with O365_USERNAME (recommended) |
| `ALLOWED_SENDERS` | `1@some.local,2@some.local` | Exact sender addresses permitted |
| `ALLOWED_SENDER_DOMAINS` | `some.local` | Whole domains permitted (any `@domain`) |
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

1. Legacy service sends an email with `From: 1@some.local` (or `2@some.local`).
2. The relay accepts the connection (IP + sender checks pass).
3. Before forwarding, the relay:
   - Adds `X-Original-From: 1@some.local` and `X-Original-To: <whatever>` headers.
   - **Rewrites `From`** to `test@some.es` (O365_USERNAME) so O365 accepts the submission.
   - Sets `Reply-To: 1@some.local` so replies go back to the original sender.
4. The relay connects to `smtp.office365.com:587`, authenticates, and delivers to `test@some.es`.
5. The user at `test@some.es` receives the email and can see the original sender in `Reply-To` / `X-Original-From`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `451 4.7.0 Upstream authentication failure` | Wrong credentials or SMTP AUTH not enabled | Check `O365_USERNAME`/`O365_PASSWORD`, enable SMTP AUTH on the account |
| `550 5.7.1 Client not authorized` | Legacy server IP not in allow-list | Add the server IP to `ALLOWED_CLIENT_IPS` in `.env` |
| `550 5.7.1 Sender not allowed` | Sender address/domain not in allow-list | Add it to `ALLOWED_SENDERS` or `ALLOWED_SENDER_DOMAINS` |
| Connection refused on port 25 | Port in use or permission denied | Change `LISTEN_PORT=2525` in `.env`; on Windows run as Administrator for port 25 |
| `535 5.7.139 … SmtpClientAuthentication is disabled` | Tenant-wide SMTP AUTH is off | Enable it in the Exchange admin center or via PowerShell (see Prerequisites) |

Enable `LOG_LEVEL=DEBUG` for detailed SMTP conversation logs when diagnosing issues.

---

## Security considerations

- The relay is **not an open relay**: it rejects connections from IPs not in `ALLOWED_CLIENT_IPS` and mail from senders not matching `ALLOWED_SENDERS` / `ALLOWED_SENDER_DOMAINS`.
- O365 credentials are **never logged** regardless of log level.
- All traffic to O365 is encrypted with **TLS via STARTTLS**.
- Connections from legacy services to this relay are **plain SMTP** (internal network only) — acceptable since they originate from the same LAN/VLAN.
- Store the `.env` file securely and exclude it from version control (add `.env` to `.gitignore`).
