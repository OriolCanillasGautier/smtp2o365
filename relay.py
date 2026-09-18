#!/usr/bin/env python3
"""
SMTP Relay for Office 365
=========================
Listens for inbound SMTP connections from legacy services (e.g., old SQL Reporting
Services, ERP mailers) and re-delivers each message to Office 365 via one of two modes:

  smtp_auth   — SMTP AUTH + STARTTLS on smtp.office365.com:587 (user + password)
  oauth2_graph — OAuth2 Client Credentials → Microsoft Graph API /sendMail
                 (Entra app registration with client secret, no SMTP AUTH needed)

Multi-account
-------------
One or more O365 sender accounts can be configured (``O365_USERNAME`` plus
``O365_USERNAME_2``, ``O365_USERNAME_3``, …).  Each incoming message is routed to
the account that matches its envelope sender:

  1. an account whose address equals the sender,
  2. an account whose ``O365_SEND_AS_<n>`` alias list contains the sender,
  3. an account whose ``O365_SEND_AS_DOMAINS_<n>`` list contains the sender's domain,
  4. otherwise the account marked ``O365_DEFAULT_ACCOUNT=n`` (default: the first one).

All configuration is driven by environment variables (or a .env file in the
same directory).  See .env.example for full documentation.
"""

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from email import message_from_bytes
from email.generator import BytesGenerator
from io import BytesIO
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosmtplib
from aiosmtpd.controller import Controller
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("smtp-relay")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_HOST: str = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT: int = int(os.getenv("LISTEN_PORT", "25"))

# Delivery mode: "smtp_auth" (default) or "oauth2_graph"
AUTH_MODE: str = os.getenv("AUTH_MODE", "smtp_auth").lower()

# --- SMTP AUTH settings (smtp_auth mode) ---
O365_HOST: str = os.getenv("O365_SMTP_HOST", "smtp.office365.com")
O365_PORT: int = int(os.getenv("O365_SMTP_PORT", "587"))

# --- Azure / Graph settings (oauth2_graph mode) ---
# These are the *tenant-wide* defaults.  Individual accounts may override them
# with AZURE_TENANT_ID_<n> / AZURE_CLIENT_ID_<n> / AZURE_CLIENT_SECRET_<n>.
AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "")

# All accepted messages are forwarded to this single destination address.
# Empty string = preserve original recipients.
FORWARD_TO: str = os.getenv("FORWARD_TO", "")

# Optional per-sender/per-account redirect targets:
#   FORWARD_TO_MAP=sender1@domain.local:person.a@remote.com,sender2@domain.local:person.b@remote.com
# A mapping may be a sender address (exact) or a bare domain (matches @domain).
FORWARD_TO_MAP: Dict[str, str] = {}


def _parse_forward_map(raw: str) -> Dict[str, str]:
    """Parse a 'key:destination,key:destination' string into a dict."""
    mapping: Dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            log.warning("Ignoring malformed FORWARD_TO_MAP entry %r (expected key:value)", pair)
            continue
        key, value = pair.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key and value:
            mapping[key] = value
    return mapping


FORWARD_TO_MAP = _parse_forward_map(os.getenv("FORWARD_TO_MAP", ""))

# What to do when the sender matches no configured account and no default is set.
#   fallback (default) — use the first configured account
#   strict             — reject the message with 550
UNMATCHED_SENDER_POLICY: str = os.getenv("UNMATCHED_SENDER_POLICY", "fallback").lower()

# When true, the From header is replaced with the sending account's address so
# that the submission is accepted without "send-as" permissions on the mailbox.
# The original sender is preserved in the Reply-To and X-Original-From headers.
REWRITE_FROM: bool = os.getenv("REWRITE_FROM", "true").lower() in ("1", "true", "yes")


def _csv_set(env_key: str, default: str) -> Set[str]:
    return {v.strip().lower() for v in os.getenv(env_key, default).split(",") if v.strip()}


def _csv_list(env_key: str, default: str = "") -> List[str]:
    return [v.strip().lower() for v in os.getenv(env_key, default).split(",") if v.strip()]


# Explicit sender addresses that are allowed to submit mail.
ALLOWED_SENDERS: Set[str] = _csv_set("ALLOWED_SENDERS", "1@some.local,2@some.local")

# Sender domains that are allowed (every address @domain is accepted).
ALLOWED_DOMAINS: Set[str] = _csv_set("ALLOWED_SENDER_DOMAINS", "some.local")

# Client IP addresses that may connect to this relay.
# Setting this empty disables IP filtering (use only in isolated networks).
ALLOWED_IPS: Set[str] = _csv_set("ALLOWED_CLIENT_IPS", "127.0.0.1,::1")

# ---------------------------------------------------------------------------
# Sender accounts
# ---------------------------------------------------------------------------


def strip_subaddress(addr: str) -> str:
    """Drop an Exchange sub-address tag: relay+tag@x → relay@x."""
    local, sep, domain = addr.partition("@")
    if sep and "+" in local:
        local = local.split("+", 1)[0]
        return f"{local}@{domain}"
    return addr


@dataclass
class Account:
    """One O365 mailbox the relay can authenticate as / send from."""

    key: str                      # "1" for O365_USERNAME, "2" for O365_USERNAME_2, …
    email: str                    # mailbox UPN / SMTP address
    password: str = ""            # smtp_auth mode
    tenant_id: str = ""           # oauth2_graph mode (falls back to AZURE_TENANT_ID)
    client_id: str = ""           # oauth2_graph mode (falls back to AZURE_CLIENT_ID)
    client_secret: str = ""       # oauth2_graph mode (falls back to AZURE_CLIENT_SECRET)
    send_as: List[str] = field(default_factory=list)           # extra local senders handled by this mailbox
    send_as_domains: List[str] = field(default_factory=list)   # whole domains handled by this mailbox
    forward_to: str = ""          # per-account redirect target (falls back to FORWARD_TO)
    is_default: bool = False

    @property
    def gtenant(self) -> str:
        return self.tenant_id or AZURE_TENANT_ID

    @property
    def gclient(self) -> str:
        return self.client_id or AZURE_CLIENT_ID

    @property
    def gsecret(self) -> str:
        return self.client_secret or AZURE_CLIENT_SECRET

    def matches(self, sender: str) -> bool:
        """Return True if this account should handle mail from ``sender``."""
        addr = (sender or "").lower().strip()
        if not addr:
            return False
        if addr == self.email or strip_subaddress(addr) == self.email:
            # The second test accepts Exchange sub-addressing (relay+tag@…).
            return True
        if addr in self.send_as:
            return True
        if "@" in addr and addr.split("@", 1)[-1] in self.send_as_domains:
            return True
        return False

    def recipient_for(self, sender: str) -> str:
        """Resolve the (single) redirect destination for mail from ``sender``.

        Precedence: FORWARD_TO_MAP (exact sender, then domain) → per-account
        forward_to → global FORWARD_TO → "" (preserve original recipients).
        """
        addr = (sender or "").lower().strip()
        if addr:
            for candidate in (addr, strip_subaddress(addr)):
                if candidate in FORWARD_TO_MAP:
                    return FORWARD_TO_MAP[candidate]
        if "@" in addr:
            domain = addr.split("@", 1)[-1]
            if domain in FORWARD_TO_MAP:
                return FORWARD_TO_MAP[domain]
        return self.forward_to or FORWARD_TO


_ACCOUNT_KEY_RE = re.compile(r"^O365_USERNAME(?:_(\d+))?$")

def _account_keys() -> List[str]:
    """Return account keys found in the environment, ordered and default first."""
    keys: List[str] = []
    for name in os.environ:
        match = _ACCOUNT_KEY_RE.match(name)
        if match and os.environ[name].strip():
            keys.append(match.group(1) or "1")

    def sort_key(key: str) -> Tuple[int, int]:
        try:
            return (0, int(key))
        except ValueError:
            return (1, 0)

    keys = sorted(set(keys), key=sort_key)
    if not keys:
        keys = ["1"]  # keeps "missing configuration" reporting meaningful

    explicit = os.getenv("O365_DEFAULT_ACCOUNT", "").strip()
    if explicit and explicit in keys:
        keys.remove(explicit)
        keys.insert(0, explicit)
    return keys


def _build_accounts() -> List[Account]:
    """Build the account list from O365_USERNAME[_n]-style environment variables."""
    accounts: List[Account] = []
    for key in _account_keys():
        suffix = "" if key == "1" else f"_{key}"

        def get(prefix: str) -> str:
            # Per-account value first, then the shared (suffix-less) fallback.
            if suffix and os.getenv(prefix + suffix, "").strip():
                return os.getenv(prefix + suffix, "").strip()
            return os.getenv(prefix, "").strip()

        email = os.getenv(f"O365_USERNAME{suffix}", "").strip().lower()
        if not email:
            continue

        accounts.append(
            Account(
                key=key,
                email=email,
                password=get("O365_PASSWORD"),
                tenant_id=os.getenv(f"AZURE_TENANT_ID{suffix}", "").strip(),
                client_id=os.getenv(f"AZURE_CLIENT_ID{suffix}", "").strip(),
                client_secret=os.getenv(f"AZURE_CLIENT_SECRET{suffix}", "").strip(),
                send_as=_csv_list(f"O365_SEND_AS{suffix}"),
                send_as_domains=_csv_list(f"O365_SEND_AS_DOMAINS{suffix}"),
                forward_to=os.getenv(f"FORWARD_TO{suffix}", "").strip(),
            )
        )

    if accounts:
        accounts[0].is_default = True
    return accounts


ACCOUNTS: List[Account] = _build_accounts()

# ---------------------------------------------------------------------------
# OAuth2 token cache (oauth2_graph mode)
# ---------------------------------------------------------------------------

_msal_apps: Dict[Tuple[str, str, str], Any] = {}


def _get_msal_app(account: Account):
    """Return a (cached) MSAL confidential client for the account's app registration."""
    cache_key = (account.gtenant, account.gclient, account.gsecret)
    app = _msal_apps.get(cache_key)
    if app is None:
        import msal  # noqa: PLC0415 — optional dependency, only needed in oauth2_graph mode
        app = msal.ConfidentialClientApplication(
            account.gclient,
            authority=f"https://login.microsoftonline.com/{account.gtenant}",
            client_credential=account.gsecret,
        )
        _msal_apps[cache_key] = app
    return app


async def _get_access_token(account: Account) -> str:
    """Acquire (or return cached) an OAuth2 access token for Microsoft Graph."""
    app = _get_msal_app(account)
    # MSAL handles in-memory caching and automatic renewal per app registration.
    result = app.acquire_token_silent(["https://graph.microsoft.com/.default"], account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(
            f"OAuth2 token acquisition failed for {account.email}: "
            f"{result.get('error_description', result)}"
        )
    return result["access_token"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sender_allowed(address: str) -> bool:
    """Return True if the envelope sender is in the allow-list."""
    addr = address.lower().strip()
    if addr in ALLOWED_SENDERS:
        return True
    domain = addr.split("@", 1)[-1] if "@" in addr else ""
    return domain in ALLOWED_DOMAINS


def select_account(sender: str) -> Optional[Account]:
    """Pick the O365 account that should deliver mail from ``sender``."""
    if not ACCOUNTS:
        return None
    for account in ACCOUNTS:
        if account.matches(sender):
            return account
    if UNMATCHED_SENDER_POLICY == "strict":
        return None
    return ACCOUNTS[0]


def _rewrite_from(msg, account: Account, original_from: str) -> None:
    """Replace the From header with the sending account's mailbox address."""
    while "From" in msg:
        del msg["From"]
    msg["From"] = account.email
    if "Reply-To" not in msg:
        msg["Reply-To"] = original_from


def _prepare_message(envelope, account: Account):
    """Parse raw MIME, add traceability headers, optionally rewrite From."""
    msg = message_from_bytes(envelope.content)

    original_from = msg.get("From", envelope.mail_from)
    msg["X-Original-From"] = original_from
    msg["X-Original-To"] = msg.get("To", ", ".join(envelope.rcpt_tos))
    msg["X-Relayed-By"] = "smtp-relay"
    msg["X-Relay-Account"] = account.email

    if REWRITE_FROM:
        _rewrite_from(msg, account, original_from)

    return msg


def _header_addresses(value: str) -> List[str]:
    """Extract bare lower-case addresses out of a header value."""
    return [a.lower() for a in re.findall(r"[\w.+=%'-]+@[\w.-]+\.\w+", value or "")]


def _recipients_for(envelope, account: Account) -> List[str]:
    """Resolve the real SMTP envelope recipients (redirect target or originals)."""
    target = account.recipient_for(envelope.mail_from)
    if target:
        return [target]

    # Preserve the original recipients, minus the relay account itself (which
    # only appears because the From header was rewritten to it).
    seen: Set[str] = set()
    recipients: List[str] = []
    for rcpt in envelope.rcpt_tos:
        addr = rcpt.strip()
        key = addr.lower()
        if not addr or key == account.email or key in seen:
            continue
        seen.add(key)
        recipients.append(addr)
    return recipients


async def relay_via_smtp(envelope, account: Account) -> None:
    """Re-deliver the received message through Office 365 SMTP AUTH + STARTTLS."""
    msg = _prepare_message(envelope, account)

    recipients = _recipients_for(envelope, account)
    if not recipients:
        raise RuntimeError("No recipients left after routing — check FORWARD_TO / recipient addresses")

    await aiosmtplib.send(
        msg,
        hostname=O365_HOST,
        port=O365_PORT,
        start_tls=True,               # STARTTLS on port 587 — required by O365
        username=account.email,
        password=account.password,
        sender=account.email,         # SMTP envelope MAIL FROM
        recipients=recipients,        # SMTP envelope RCPT TO
        timeout=30,
    )


async def relay_via_graph(envelope, account: Account) -> None:
    """Re-deliver the received message through Microsoft Graph API /sendMail."""
    import httpx  # noqa: PLC0415 — optional dependency, only needed in oauth2_graph mode

    msg = _prepare_message(envelope, account)

    target = account.recipient_for(envelope.mail_from)
    if target:
        # Redirect mode: a single destination replaces all To/Cc recipients.
        for header in ("To", "Cc", "Bcc"):
            while header in msg:
                del msg[header]
        msg["To"] = target
    else:
        # Preserve recipients, but drop the relay account itself when the From
        # header was rewritten to it (it is the sender, not a recipient).
        for header in ("To", "Cc"):
            if header not in msg:
                continue
            value = msg[header]
            addrs = _header_addresses(value)
            if not addrs or account.email in addrs:
                while header in msg:
                    del msg[header]
        if "To" not in msg:
            keep = [r for r in envelope.rcpt_tos if r.strip().lower() != account.email]
            if keep:
                msg["To"] = ", ".join(keep)

    # Graph /sendMail rejects a message with no recipients at all.
    if "To" not in msg and "Cc" not in msg and "Bcc" not in msg:
        raise RuntimeError("No recipients left after routing — check FORWARD_TO / recipient addresses")

    # Serialise the modified message back to MIME bytes.
    buf = BytesIO()
    BytesGenerator(buf, mangle_from_=False).flatten(msg)
    mime_bytes = buf.getvalue()

    token = await _get_access_token(account)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://graph.microsoft.com/v1.0/users/{account.email}/sendMail",
            content=base64.b64encode(mime_bytes),
            headers={
                "Authorization": f"Bearer {token}",
                # Graph /sendMail MIME endpoint requires base64-encoded content
                "Content-Type": "text/plain",
            },
        )

    if response.status_code != 202:
        raise RuntimeError(
            f"Graph API /sendMail returned {response.status_code} for {account.email}: "
            f"{response.text[:500]}"
        )


# ---------------------------------------------------------------------------
# SMTP handler
# ---------------------------------------------------------------------------


class RelayHandler:
    """aiosmtpd handler that enforces allow-lists and relays accepted mail."""

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        peer = session.peer[0]

        # Reject connections from unexpected client IPs.
        if ALLOWED_IPS and peer not in ALLOWED_IPS:
            log.warning("Rejected connection from %s — not in ALLOWED_CLIENT_IPS", peer)
            return "550 5.7.1 Client not authorized"

        # Reject mail from addresses / domains not in the allow-list.
        if not sender_allowed(address):
            log.warning("Rejected MAIL FROM <%s> — sender not allowed", address)
            return "550 5.7.1 Sender not allowed"

        # Reject mail whose sender cannot be mapped to a sending account.
        if select_account(address) is None:
            log.warning("Rejected MAIL FROM <%s> — no account configured for this sender", address)
            return "550 5.7.1 Sender has no relay account"

        # aiosmtpd only updates the envelope when the hook returns MISSING (no hook).
        # Since we define this hook, we must update the envelope manually.
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        # Accept any RCPT TO — filtering happens at MAIL FROM level.
        # aiosmtpd only updates the envelope when the hook returns MISSING (no hook),
        # so we must append the recipient manually.
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        account = select_account(envelope.mail_from)
        if account is None:
            log.error("No account for sender <%s> — rejecting", envelope.mail_from)
            return "550 5.7.1 Sender has no relay account"

        destination = account.recipient_for(envelope.mail_from)

        log.info(
            "Relaying  from=<%s>  via=<%s>  orig_to=%s  dest=%s  peer=%s",
            envelope.mail_from,
            account.email,
            envelope.rcpt_tos,
            destination or "(original recipients)",
            session.peer[0],
        )

        try:
            if AUTH_MODE == "oauth2_graph":
                await relay_via_graph(envelope, account)
            else:
                await relay_via_smtp(envelope, account)

            log.info("Delivered via <%s> → %s", account.email, destination or "original recipients")
            return "250 2.0.0 Message accepted for delivery"

        except aiosmtplib.SMTPAuthenticationError as exc:
            log.error("O365 authentication failed for %s: %s", account.email, exc)
            return "451 4.7.0 Upstream authentication failure"

        except aiosmtplib.SMTPException as exc:
            log.error("O365 SMTP error: %s", exc)
            return "451 4.4.1 Upstream relay failure, try again later"

        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected relay error: %s", exc)
            return "451 4.3.0 Internal relay error"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_configuration() -> List[str]:
    """Return a list of fatal configuration problems (empty = OK)."""
    problems: List[str] = []

    if not ACCOUNTS:
        problems.append("O365_USERNAME is not set (no sending account configured)")
        return problems

    seen_emails: Dict[str, str] = {}

    for account in ACCOUNTS:
        label = f"O365_USERNAME{'' if account.key == '1' else '_' + account.key}"

        if account.email in seen_emails:
            problems.append(
                f"{label} duplicates the address of {seen_emails[account.email]} "
                f"({account.email}) — give each account a distinct mailbox"
            )
        else:
            seen_emails[account.email] = label

        if AUTH_MODE == "oauth2_graph":
            missing = [
                name
                for name, value in (
                    ("AZURE_TENANT_ID", account.gtenant),
                    ("AZURE_CLIENT_ID", account.gclient),
                    ("AZURE_CLIENT_SECRET", account.gsecret),
                )
                if not value
            ]
            if missing:
                problems.append(
                    f"{label} <{account.email}>: missing {', '.join(missing)}"
                )
        else:
            if not account.password:
                problems.append(
                    f"{label} <{account.email}>: missing O365_PASSWORD"
                    + ("" if account.key == "1" else f" (or O365_PASSWORD_{account.key})")
                )

        # Warn (do not fail) about aliases that the allow-list would reject anyway.
        for alias in account.send_as:
            if not sender_allowed(alias):
                log.warning(
                    "Account <%s> lists send-as %s but it is not in ALLOWED_SENDERS/"
                    "ALLOWED_SENDER_DOMAINS — such mail will be rejected",
                    account.email, alias,
                )
        for domain in account.send_as_domains:
            if domain not in ALLOWED_DOMAINS:
                log.warning(
                    "Account <%s> lists send-as domain %s but it is not in "
                    "ALLOWED_SENDER_DOMAINS — such mail will be rejected",
                    account.email, domain,
                )

    if UNMATCHED_SENDER_POLICY not in ("fallback", "strict"):
        problems.append(
            f"UNMATCHED_SENDER_POLICY must be 'fallback' or 'strict' "
            f"(got {UNMATCHED_SENDER_POLICY!r})"
        )

    return problems


def main() -> None:
    # Validate required configuration before starting.
    problems = _validate_configuration()
    if problems:
        for problem in problems:
            log.critical("Configuration error: %s", problem)
        raise SystemExit(1)

    log.info("SMTP-Relay starting  (auth mode: %s)", AUTH_MODE)
    log.info("  Listening on     : %s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("  Accounts         : %d", len(ACCOUNTS))
    for account in ACCOUNTS:
        extra: List[str] = []
        if account.send_as:
            extra.append(f"send-as: {', '.join(account.send_as)}")
        if account.send_as_domains:
            extra.append(f"domains: {', '.join(account.send_as_domains)}")
        if account.forward_to:
            extra.append(f"forward-to: {account.forward_to}")
        suffix = f"  [{'; '.join(extra)}]" if extra else ""
        log.info(
            "    - %-30s %s%s",
            account.email,
            "(default)" if account.is_default else "",
            suffix,
        )
    if FORWARD_TO:
        log.info("  Forward to       : <%s>", FORWARD_TO)
    if FORWARD_TO_MAP:
        log.info("  Forward map      : %s", FORWARD_TO_MAP)
    if AUTH_MODE == "oauth2_graph":
        log.info("  Azure Tenant ID  : %s", AZURE_TENANT_ID)
        log.info("  Azure Client ID  : %s", AZURE_CLIENT_ID)
    else:
        log.info("  O365 SMTP        : %s:%d", O365_HOST, O365_PORT)
    log.info("  Unmatched sender : %s", UNMATCHED_SENDER_POLICY)
    log.info("  Allowed senders  : %s", ALLOWED_SENDERS or "(domain-based only)")
    log.info("  Allowed domains  : %s", ALLOWED_DOMAINS)
    log.info("  Allowed IPs      : %s", ALLOWED_IPS or "(ALL — consider restricting)")
    log.info("  Rewrite From     : %s", REWRITE_FROM)

    controller = Controller(RelayHandler(), hostname=LISTEN_HOST, port=LISTEN_PORT)
    controller.start()
    log.info("Ready. Waiting for connections…")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupt received, shutting down…")
    finally:
        controller.stop()
        log.info("SMTP-Relay stopped.")


if __name__ == "__main__":
    main()
