#!/usr/bin/env python3
"""
SMTP Relay for Office 365
=========================
Listens for inbound SMTP connections from legacy services (e.g., old SQL Reporting
Services, ERP mailers) and re-delivers each message to Office 365 via SMTP AUTH
with STARTTLS on smtp.office365.com:587.

All configuration is driven by environment variables (or a .env file in the
same directory).  See .env.example for full documentation.
"""

import logging
import os
import time
from email import message_from_bytes
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

O365_HOST: str = os.getenv("O365_SMTP_HOST", "smtp.office365.com")
O365_PORT: int = int(os.getenv("O365_SMTP_PORT", "587"))
O365_USER: str = os.getenv("O365_USERNAME", "")
O365_PASS: str = os.getenv("O365_PASSWORD", "")

# All accepted messages are forwarded to this single destination address.
FORWARD_TO: str = os.getenv("FORWARD_TO", "test@some.es")

# When true, the From header is replaced with O365_USER so that the O365
# SMTP AUTH submission is accepted without "send-as" permissions.
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
# Helpers
# ---------------------------------------------------------------------------


def sender_allowed(address: str) -> bool:
    """Return True if the envelope sender is in the allow-list."""
    addr = address.lower().strip()
    if addr in ALLOWED_SENDERS:
        return True
    domain = addr.split("@", 1)[-1] if "@" in addr else ""
    return domain in ALLOWED_DOMAINS


async def relay_to_o365(envelope) -> None:
    """Re-deliver the received message through Office 365 SMTP AUTH."""
    msg = message_from_bytes(envelope.content)

    # ---- Traceability headers ------------------------------------------------
    original_from = msg.get("From", envelope.mail_from)
    msg["X-Original-From"] = original_from
    msg["X-Original-To"] = msg.get("To", ", ".join(envelope.rcpt_tos))
    msg["X-Relayed-By"] = "smtp-relay"
    # --------------------------------------------------------------------------

    if REWRITE_FROM:
        # O365 SMTP AUTH requires the RFC 5321 MAIL FROM to match (or be
        # permitted by) the authenticated account.  Rewrite the visible From
        # and keep the original sender addressable via Reply-To.
        while "From" in msg:
            del msg["From"]
        msg["From"] = O365_USER
        if "Reply-To" not in msg:
            msg["Reply-To"] = original_from

    await aiosmtplib.send(
        msg,
        hostname=O365_HOST,
        port=O365_PORT,
        start_tls=True,          # STARTTLS on port 587 — required by O365
        username=O365_USER,
        password=O365_PASS,
        sender=O365_USER,        # SMTP envelope MAIL FROM
        recipients=[FORWARD_TO], # SMTP envelope RCPT TO
        timeout=30,
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

        return "250 OK"  # aiosmtpd sets envelope.mail_from automatically on 250

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        # Accept any RCPT TO — filtering happens at MAIL FROM level.
        # aiosmtpd appends the address to envelope.rcpt_tos automatically on 250.
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        log.info(
            "Relaying  from=<%s>  orig_to=%s  peer=%s",
            envelope.mail_from,
            envelope.rcpt_tos,
            session.peer[0],
        )

        try:
            await relay_to_o365(envelope)
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
    if not O365_USER or not O365_PASS:
        log.critical("O365_USERNAME and O365_PASSWORD must be set (check your .env file)")
        raise SystemExit(1)

    log.info("SMTP-Relay starting")
    log.info("  Listening on     : %s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("  Forward to       : <%s>", FORWARD_TO)
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
