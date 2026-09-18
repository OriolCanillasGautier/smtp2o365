# SMTP Relay for Office 365

A lightweight SMTP relay written in Python that accepts mail from **legacy services** (old SQL Reporting Services, ERP mailers, line-of-business apps) that cannot use modern authentication, and re-delivers every message to Office 365 through one or more sending mailboxes.

| Mode | When to use |
|---|---|
| `smtp_auth` | SMTP AUTH + STARTTLS — user account with password or App Password |
| `oauth2_graph` | Microsoft Graph API — Entra app registration with client secret (recommended when SMTP AUTH / App Passwords are unavailable) |

```
Legacy service  ──SMTP──►  smtp-relay (this server)  ──STARTTLS/AUTH──►  smtp.office365.com  ──►  destination@domain.com
(no modern auth)             listens on :25               [smtp_auth mode]        port 587
e.g. 1@domain.local
     2@domain.local                                  OR

                             routes by sender ────────►  ──Graph API HTTPS──►  graph.microsoft.com  ──►  destination@domain.com
                                                          [oauth2_graph mode]
                                                          /users/relay1@contoso.com/sendMail
                                                          /users/relay2@contoso.com/sendMail
```

**Two things this relay can do that a plain forwarder cannot:**

1. **Multiple sending accounts** — configure `O365_USERNAME`, `O365_USERNAME_2`, `O365_USERNAME_3` … and each local sender is automatically routed to the right O365 mailbox.
2. **Per-sender redirection** — with `FORWARD_TO_MAP`, mail from one local address can be delivered to one specific remote address, another local address to a different one, while everything else keeps its original recipients.

---

## Prerequisites

### Option A — `smtp_auth` mode (SMTP AUTH + STARTTLS)

1. **Enable SMTP AUTH** for each O365 sending account.
   - Microsoft 365 admin center → **Users** → **Active users** → select account → **Mail** tab → **Manage email apps** → tick **Authenticated SMTP**.
   - If the tenant-wide policy is also off, a Global Admin must run:
     ```powershell
     Set-TransportConfig -SmtpClientAuthenticationDisabled $false
     ```
2. **Get a password** for each sending account.

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
5. *(Strongly recommended)* restrict the app to only the mailboxes it must send from — see [Locking the app down to specific mailboxes](#locking-the-app-down-to-specific-mailboxes).

> **One app registration is enough for several mailboxes in the same tenant.** See below.

---

## Quick start — Docker (recommended)

```bash
# 1. Clone / copy the project
cd smtp2o365

# 2. Create the config file
copy .env.example .env
# Edit .env — set AUTH_MODE and your account(s):
#   smtp_auth    → O365_USERNAME(+_2, _3…) and O365_PASSWORD(+_2, _3…)
#   oauth2_graph → O365_USERNAME(+_2, _3…) and the AZURE_* variables

# 3. Start the relay
docker compose up -d

# 4. Watch the logs — the startup banner lists every account and its routing
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

## Multiple sending accounts

### How it works

Every incoming message has an envelope sender (`MAIL FROM:`), for example `1@some.local`. The relay maps that sender to one of the configured O365 mailboxes:

| Priority | Rule | Configured with |
|---|---|---|
| 1 | The sender **is** the account address | `O365_USERNAME_<n>` |
| 2 | The sender is listed as an alias of the account | `O365_SEND_AS_<n>` |
| 3 | The sender's **domain** is handled by the account | `O365_SEND_AS_DOMAINS_<n>` |
| 4 | Nothing matched → the default account | `O365_DEFAULT_ACCOUNT` (default: account 1) |

If nothing matches and `UNMATCHED_SENDER_POLICY=strict`, the message is rejected with `550 5.7.1 Sender has no relay account` instead. The default is `fallback`, which uses the default account.

### Configuration

```env
# Account 1 — always the base name
O365_USERNAME=relay1@contoso.com
O365_SEND_AS=1@some.local
O365_SEND_AS_DOMAINS=some.local

# Account 2
O365_USERNAME_2=relay2@contoso.com
O365_SEND_AS_2=2@some.local,printer@some.local

# Account 3, 4, … — same pattern
# O365_USERNAME_3=relay3@contoso.com

# Account used when the sender matches no rule (default = 1)
O365_DEFAULT_ACCOUNT=1
UNMATCHED_SENDER_POLICY=fallback
```

With that configuration:

| Legacy service sends as | Delivered as (From) | Auth account |
|---|---|---|
| `1@some.local` | `relay1@contoso.com` | `relay1@contoso.com` |
| `anything@some.local` | `relay1@contoso.com` | `relay1@contoso.com` |
| `2@some.local` | `relay2@contoso.com` | `relay2@contoso.com` |
| `printer@some.local` | `relay2@contoso.com` | `relay2@contoso.com` |
| `relay2@contoso.com` | `relay2@contoso.com` | `relay2@contoso.com` |
| `unknown@elsewhere.local` | `relay1@contoso.com` | `relay1@contoso.com` (fallback) |

> Add every address listed in `O365_SEND_AS_<n>` to `ALLOWED_SENDERS` (and every domain in `O365_SEND_AS_DOMAINS_<n>` to `ALLOWED_SENDER_DOMAINS`). The relay logs a warning at startup if an alias would be blocked by the allow-list.

### Do I need two OAuth tokens for two accounts in the same tenant?

**No.** In `oauth2_graph` mode the token belongs to the **Entra application**, not to a mailbox:

- one `AZURE_TENANT_ID`, one `AZURE_CLIENT_ID`, one `AZURE_CLIENT_SECRET`,
- one Microsoft Graph **application** permission `Mail.Send` with admin consent,
- the sending mailbox is selected purely by the request URL:

  ```
  POST https://graph.microsoft.com/v1.0/users/<O365_USERNAME_<n>>/sendMail
  ```

The relay keeps **one MSAL client and one cached token per app registration**, not per mailbox. In the common case (all accounts in one tenant, one app registration) all accounts share a single token.

Per-account `AZURE_*_<n>` overrides exist only for the unusual cases: an account in a **different tenant**, or in the same tenant but deliberately using a **separate app registration** (for example so each account can be restricted independently).

### Locking the app down to specific mailboxes

`Mail.Send` as an **application** permission allows the app to send as *any* mailbox in the tenant. Restrict it to the mailboxes you actually need with an Exchange Online **application access policy**:

```powershell
# 1. Create a mail-enabled security group containing ONLY the sending mailboxes
New-DistributionGroup -Name "SMTP Relay Senders" -Type Security -Members relay1@contoso.com,relay2@contoso.com

# 2. Restrict the app to that group
New-ApplicationAccessPolicy -AppId <AZURE_CLIENT_ID> `
  -PolicyScopeGroupId "SMTP Relay Senders" `
  -AccessRight RestrictAccess `
  -Description "Restrict smtp-relay to its sending mailboxes"

# 3. Verify (AccessCheckResult should be Granted for the two mailboxes, Denied for others)
Test-ApplicationAccessPolicy -Identity relay1@contoso.com -AppId <AZURE_CLIENT_ID>
Test-ApplicationAccessPolicy -Identity someone.else@contoso.com -AppId <AZURE_CLIENT_ID>
```

A tenant may instead use the newer **Application RBAC for Exchange Online** model; either is fine — the goal is the same.

---

## Sending one local sender to a specific remote address

Three levels of recipient handling, most specific first:

| Setting | Scope |
|---|---|
| `FORWARD_TO_MAP` | per sender address **or** per sender domain |
| `FORWARD_TO_<n>` | per sending account |
| `FORWARD_TO` | global default |
| *(none set)* | original `To`/`Cc` recipients are preserved |

```env
# Mail from 1@some.local → person.a@remote.com
# Mail from 2@some.local → person.b@remote.com
FORWARD_TO_MAP=1@some.local:person.a@remote.com,2@some.local:person.b@remote.com

# Everything else goes here
FORWARD_TO=fallback@remote.com
```

A bare domain as the key matches every sender in that domain:

```env
FORWARD_TO_MAP=some.local:helpdesk@remote.com
```

In redirect mode (`FORWARD_TO` / `FORWARD_TO_<n>` / `FORWARD_TO_MAP`) the relay:

- rewrites `To` to the single destination,
- **removes `Cc`/`Bcc`** — otherwise O365 would still deliver copies to those addresses,
- keeps the original recipients readable in the `X-Original-To` header,
- keeps the original sender in `Reply-To` and `X-Original-From`.

Exchange sub-addressing is understood: `1+weekly@some.local` matches the map key `1@some.local`, and an account address such as `relay2@contoso.com` also matches `relay2+anything@contoso.com`.

If a redirect target can never be resolved and no recipients remain, the message is deferred with `451` rather than silently dropped.

---

## Configuration reference

All settings are in `.env` (copy from `.env.example`). `<n>` is an account number (`_2`, `_3`, …); a setting without a suffix applies to all accounts as the default.

| Variable | Default | Description |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | IP to bind the relay on |
| `LISTEN_PORT` | `25` | TCP port to listen on (`2525` if 25 is unavailable) |
| `AUTH_MODE` | `smtp_auth` | `smtp_auth` or `oauth2_graph` — see above |
| `O365_USERNAME` | — | **Required.** Mailbox for account 1 |
| `O365_USERNAME_<n>` | — | **Optional.** Mailbox for account *n* (enables multi-account routing) |
| `O365_PASSWORD` / `O365_PASSWORD_<n>` | — | **smtp_auth only.** Account password or App Password |
| `O365_SEND_AS_<n>` | — | Extra local sender addresses routed to account *n* |
| `O365_SEND_AS_DOMAINS_<n>` | — | Whole local domains routed to account *n* |
| `O365_DEFAULT_ACCOUNT` | `1` | Account number used when no rule matches |
| `UNMATCHED_SENDER_POLICY` | `fallback` | `fallback` = use default account · `strict` = reject with 550 |
| `O365_SMTP_HOST` | `smtp.office365.com` | O365 SMTP endpoint *(smtp_auth only)* |
| `O365_SMTP_PORT` | `587` | O365 SMTP port *(smtp_auth only)* |
| `AZURE_TENANT_ID` | — | **oauth2_graph.** Directory (tenant) ID — shared by all accounts |
| `AZURE_CLIENT_ID` | — | **oauth2_graph.** Application (client) ID — shared by all accounts |
| `AZURE_CLIENT_SECRET` | — | **oauth2_graph.** Client secret value — shared by all accounts |
| `AZURE_TENANT_ID_<n>` etc. | — | **Optional per-account override** (different tenant / separate app registration) |
| `FORWARD_TO` | — | Global redirect target. Empty = preserve original recipients |
| `FORWARD_TO_MAP` | — | `sender-or-domain:destination` pairs, comma-separated |
| `FORWARD_TO_<n>` | — | Redirect target for one sending account |
| `REWRITE_FROM` | `true` | Replace `From` with the sending account's address (recommended) |
| `ALLOWED_SENDERS` | `1@domain.local,2@domain.local` | Exact sender addresses permitted |
| `ALLOWED_SENDER_DOMAINS` | `domain.local` | Whole domains permitted (any `@domain`) |
| `ALLOWED_CLIENT_IPS` | `127.0.0.1,::1` | Client IPs allowed to connect. **Add your server IPs here.** Leave empty to allow all (isolated networks only). |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

At startup the relay prints one line per configured account, its routing rules and its redirect target, so `docker compose logs` is enough to confirm the routing table.

### Verifying the routing logic

`test_routing.py` checks account discovery, sender→account selection, credential inheritance, the redirect map and the startup validation without needing O365 credentials (it stubs the network libraries):

```bash
python test_routing.py
```

Run it after changing `.env`-style settings or editing `relay.py`.

---

## Configuring your legacy services

Point each legacy application's **SMTP server / smarthost** setting to the machine running this relay:

| Setting | Value |
|---|---|
| SMTP Server | `<IP of this machine>` |
| Port | `25` (or `2525` if you changed it) |
| Authentication | **None** (the relay handles O365 auth internally) |
| TLS/SSL | **None** / disabled (plain SMTP to the relay) |
| From / sender address | A local address listed in `ALLOWED_SENDERS` |

The **sender address each service uses decides which O365 account and which destination it gets.** Nothing else has to change on the legacy side.

> **Security note:** The relay intentionally accepts unauthenticated connections from legacy clients. It is protected by the `ALLOWED_CLIENT_IPS` and `ALLOWED_SENDERS` allow-lists. **Never expose port 25 to the internet** — bind it to an internal interface or restrict it with a firewall rule.

---

## What happens to each message

### Scenario A: redirect mode (`FORWARD_TO` / `FORWARD_TO_<n>` / `FORWARD_TO_MAP`)

1. Legacy service sends an email with `From: user1@domain.local` to several recipients.
2. The relay accepts the connection (IP + sender checks pass) and picks the account that handles `user1@domain.local`.
3. Before forwarding, the relay:
   - Adds `X-Original-From`, `X-Original-To` and `X-Relay-Account` headers.
   - **Rewrites `From`** to the selected account's address so O365 accepts the submission.
   - Sets `Reply-To: user1@domain.local` so replies go back to the original sender.
   - **Replaces `To`** with the mapped destination and drops `Cc`/`Bcc`.
4. The relay authenticates as that account and delivers to the single destination.
5. The recipient sees the account as the sender, the original sender in `Reply-To` / `X-Original-From`, and the original recipients in `X-Original-To`.

### Scenario B: no redirect configured (preserve recipients)

1. Legacy service sends an email with `From: user1@domain.local` to `recipient1@domain.com, recipient2@domain.com`.
2. The relay accepts the connection and picks the account that handles `user1@domain.local`.
3. Before forwarding, the relay:
   - Adds `X-Original-From`, `X-Original-To` and `X-Relay-Account` headers.
   - **Rewrites `From`** to the selected account's address.
   - Sets `Reply-To: user1@domain.local`.
   - **Preserves `To/Cc`** — removing only the relay account itself if it appeared as a recipient.
4. The relay authenticates as that account and delivers to all original recipients.
5. Each recipient receives the email as intended, with the original sender preserved in `Reply-To` / `X-Original-From`.

> With `AUTH_MODE=smtp_auth`, O365 also copies the message into the sending account's **Sent Items**. With `oauth2_graph`, Graph `/sendMail` does not save a copy unless you add that behaviour yourself.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `451 4.7.0 Upstream authentication failure` | Wrong credentials, SMTP AUTH not enabled, or the wrong `O365_PASSWORD_<n>` for that account | Check `O365_USERNAME_<n>` / `O365_PASSWORD_<n>`; enable SMTP AUTH on that account |
| `451 4.3.0 … OAuth2 token acquisition failed for <mailbox>` | Wrong tenant/client ID or secret, or missing admin consent | Verify `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`; grant admin consent on Mail.Send |
| `451 4.3.0 … Graph API /sendMail returned 403 for <mailbox>` | App lacks Mail.Send consent, or an application access policy excludes that mailbox | Grant **Mail.Send** + admin consent; check `Test-ApplicationAccessPolicy` |
| `451 4.3.0 … Graph API /sendMail returned 404 for <mailbox>` | `O365_USERNAME_<n>` is not a real mailbox in the tenant | Fix the address, or create the mailbox |
| `550 5.7.1 Client not authorized` | Legacy server IP not in allow-list | Add the server IP to `ALLOWED_CLIENT_IPS` in `.env` |
| `550 5.7.1 Sender not allowed` | Sender address/domain not in allow-list | Add it to `ALLOWED_SENDERS` / `ALLOWED_SENDER_DOMAINS` |
| `550 5.7.1 Sender has no relay account` | `UNMATCHED_SENDER_POLICY=strict` and no account matched | Add the sender to `O365_SEND_AS_<n>` / `O365_SEND_AS_DOMAINS_<n>`, or switch to `fallback` |
| Mail arrives from the **wrong** O365 account | A `O365_SEND_AS_DOMAINS_<n>` rule is broader than intended, or the sender matched the fallback | Tighten the alias/domain lists; set `UNMATCHED_SENDER_POLICY=strict` to surface mismatches instead of hiding them |
| Mail went to the original recipients instead of the redirect target | `FORWARD_TO*` not set, or the map key does not match the envelope sender | Use the exact `MAIL FROM` address or its domain as the map key; check the `dest=` field in the log line |
| `451 4.3.0 No recipients left after routing` | Redirect produced an empty recipient list | Check `FORWARD_TO*` values |
| Startup exits with "Configuration error: …" | A required variable for one of the accounts is missing | The log line names the exact variable per account |
| Connection refused on port 25 | Port in use or permission denied | Change `LISTEN_PORT=2525` in `.env`; on Windows run as Administrator for port 25 |
| `535 5.7.139 … SmtpClientAuthentication is disabled` | Tenant-wide SMTP AUTH is off | Enable it in the Exchange admin center or via PowerShell (see Prerequisites), or switch to `AUTH_MODE=oauth2_graph` |

Enable `LOG_LEVEL=DEBUG` for detailed SMTP conversation logs when diagnosing issues. Every relayed message logs its sender, the account used, the recipients and the resolved destination.

---

## Security considerations

- The relay is **not an open relay**: it rejects connections from IPs not in `ALLOWED_CLIENT_IPS`, mail from senders not matching `ALLOWED_SENDERS` / `ALLOWED_SENDER_DOMAINS`, and (in `strict` mode) senders with no configured account.
- O365 credentials are **never logged** regardless of log level.
- All traffic to O365 is encrypted with **TLS via STARTTLS** (or HTTPS for Graph).
- Graph `Mail.Send` is tenant-wide by default — pair it with an [application access policy](#locking-the-app-down-to-specific-mailboxes) so the relay can only send as the mailboxes it needs.
- A compromise of this relay lets an attacker send as the configured accounts. Keep it on the internal network and prefer `strict` routing.
- Connections from legacy services to this relay are **plain SMTP** (internal network only) — acceptable since they originate from the same LAN/VLAN.
- Store the `.env` file securely and exclude it from version control (add `.env` to `.gitignore`).
