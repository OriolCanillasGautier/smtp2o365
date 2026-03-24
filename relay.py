#!/usr/bin/env python3
"""
SMTP Relay for Office 365
=========================
Listens for inbound SMTP connections from legacy services (e.g., old SQL Reporting
Services, ERP mailers) and re-delivers each message to Office 365 via one of two modes:

  smtp_auth   — SMTP AUTH + STARTTLS on smtp.office365.com:587 (user + password)
  oauth2_graph — OAuth2 Client Credentials → Microsoft Graph API /sendMail
                 (Entra app registration with client secret, no SMTP AUTH needed)

All configuration is driven by environment variables (or a .env file in the
same directory).  See .env.example for full documentation.
"""

import base64
import logging
import os
import time
from email import message_from_bytes
from email.generator import BytesGenerator
from io import BytesIO
from typing import Set

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
O365_USER: str = os.getenv("O365_USERNAME", "")
O365_PASS: str = os.getenv("O365_PASSWORD", "")

# --- Azure / Graph settings (oauth2_graph mode) ---
AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "")

# All accepted messages are forwarded to this single destination address.
# Empty string = preserve original recipients.
FORWARD_TO: str = os.getenv("FORWARD_TO", "")

# When true, the From header is replaced with O365_USER so that the submission
# is accepted without "send-as" permissions on the mailbox.
# The original sender is preserved in the Reply-To and X-Original-From headers.
REWRITE_FROM: bool = os.getenv("REWRITE_FROM", "true").lower() in ("1", "true", "yes")


def _csv_set(env_key: str, default: str) -> Set[str]:
    return {v.strip().lower() for v in os.getenv(env_key, default).split(",") if v.strip()}


# Explicit sender addresses that are allowed to submit mail.
ALLOWED_SENDERS: Set[str] = _csv_set("ALLOWED_SENDERS", "1@some.local,2@some.local")

# Sender domains that are allowed (every address @domain is accepted).
ALLOWED_DOMAINS: Set[str] = _csv_set("ALLOWED_SENDER_DOMAINS", "some.local")

# Client IP addresses that may connect to this relay.
# Setting this empty disables IP filtering (use only in isolated networks).
ALLOWED_IPS: Set[str] = _csv_set("ALLOWED_CLIENT_IPS", "127.0.0.1,::1")

# ---------------------------------------------------------------------------
# OAuth2 token cache (oauth2_graph mode)
# ---------------------------------------------------------------------------

_msal_app = None  # Created lazily on first token request


def _get_msal_app():
    global _msal_app
    if _msal_app is None:
        import msal  # noqa: PLC0415 — optional dependency, only needed in oauth2_graph mode
        _msal_app = msal.ConfidentialClientApplication(
            AZURE_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
            client_credential=AZURE_CLIENT_SECRET,
        )
    return _msal_app


async def _get_access_token() -> str:
    """Acquire (or return cached) an OAuth2 access token for Microsoft Graph."""
    app = _get_msal_app()
    # MSAL handles in-memory caching and automatic renewal.
    result = app.acquire_token_silent(["https://graph.microsoft.com/.default"], account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(
            f"OAuth2 token acquisition failed: {result.get('error_description', result)}"
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


def _prepare_message(envelope):
    """Parse raw MIME, add traceability headers, optionally rewrite From."""
    msg = message_from_bytes(envelope.content)

    original_from = msg.get("From", envelope.mail_from)
    msg["X-Original-From"] = original_from
    msg["X-Original-To"] = msg.get("To", ", ".join(envelope.rcpt_tos))
    msg["X-Relayed-By"] = "smtp-relay"

    if REWRITE_FROM:
        while "From" in msg:
            del msg["From"]
        msg["From"] = O365_USER
        if "Reply-To" not in msg:
            msg["Reply-To"] = original_from

    return msg


async def relay_via_smtp(envelope) -> None:
    """Re-deliver the received message through Office 365 SMTP AUTH + STARTTLS."""
    msg = _prepare_message(envelope)

    # Use FORWARD_TO if defined, otherwise preserve original recipients.
    recipients = [FORWARD_TO] if FORWARD_TO else envelope.rcpt_tos

    await aiosmtplib.send(
        msg,
        hostname=O365_HOST,
        port=O365_PORT,
        start_tls=True,          # STARTTLS on port 587 — required by O365
        username=O365_USER,
        password=O365_PASS,
        sender=O365_USER,        # SMTP envelope MAIL FROM
        recipients=recipients,   # SMTP envelope RCPT TO
        timeout=30,
    )


async def relay_via_graph(envelope) -> None:
    """Re-deliver the received message through Microsoft Graph API /sendMail."""
    import httpx  # noqa: PLC0415 — optional dependency, only needed in oauth2_graph mode

    msg = _prepare_message(envelope)

    # If FORWARD_TO is defined, override To header. Otherwise preserve original recipients.
    if FORWARD_TO:
        while "To" in msg:
            del msg["To"]
        msg["To"] = FORWARD_TO

    # Serialise the modified message back to MIME bytes.
    buf = BytesIO()
    BytesGenerator(buf, mangle_from_=False).flatten(msg)
    mime_bytes = buf.getvalue()

    token = await _get_access_token()

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://graph.microsoft.com/v1.0/users/{O365_USER}/sendMail",
            content=base64.b64encode(mime_bytes),
            headers={
                "Authorization": f"Bearer {token}",
                # Graph /sendMail MIME endpoint requires base64-encoded content
                "Content-Type": "text/plain",
            },
        )

    if response.status_code != 202:
        raise RuntimeError(
            f"Graph API /sendMail returned {response.status_code}: {response.text[:500]}"
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
        log.info(
            "Relaying  from=<%s>  orig_to=%s  peer=%s",
            envelope.mail_from,
            envelope.rcpt_tos,
            session.peer[0],
        )

        try:
            if AUTH_MODE == "oauth2_graph":
                await relay_via_graph(envelope)
            else:
                await relay_via_smtp(envelope)

            log.info("Delivered → <%s>", FORWARD_TO)
            return "250 2.0.0 Message accepted for delivery"

        except aiosmtplib.SMTPAuthenticationError as exc:
            log.error("O365 authentication failed: %s", exc)
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


def main() -> None:
    # Validate required configuration before starting.
    if AUTH_MODE == "oauth2_graph":
        missing = [
            k for k, v in {
                "O365_USERNAME": O365_USER,
                "AZURE_TENANT_ID": AZURE_TENANT_ID,
                "AZURE_CLIENT_ID": AZURE_CLIENT_ID,
                "AZURE_CLIENT_SECRET": AZURE_CLIENT_SECRET,
            }.items() if not v
        ]
    else:
        missing = [
            k for k, v in {
                "O365_USERNAME": O365_USER,
                "O365_PASSWORD": O365_PASS,
            }.items() if not v
        ]

    if missing:
        log.critical("Missing required environment variable(s): %s", ", ".join(missing))
        raise SystemExit(1)

    log.info("SMTP-Relay starting  (auth mode: %s)", AUTH_MODE)
    log.info("  Listening on     : %s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("  Forward to       : <%s>", FORWARD_TO)
    if AUTH_MODE == "oauth2_graph":
        log.info("  Graph API sender : <%s>", O365_USER)
        log.info("  Azure Tenant ID  : %s", AZURE_TENANT_ID)
        log.info("  Azure Client ID  : %s", AZURE_CLIENT_ID)
    else:
        log.info("  O365 SMTP        : %s:%d  (auth user: <%s>)", O365_HOST, O365_PORT, O365_USER)
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
