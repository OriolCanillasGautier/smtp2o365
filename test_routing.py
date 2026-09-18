"""Functional tests for multi-account routing in relay.py.

Run with:  python test_routing.py

The relay's runtime-only dependencies (aiosmtplib, aiosmtpd, dotenv) are stubbed
so the configuration, routing and message-rewriting logic can be exercised for
real without installing the full stack.  Exit code 0 = all checks passed.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_test"))

# ---------------------------------------------------------------------------
# Stub the runtime-only dependencies so relay.py can be imported.
# ---------------------------------------------------------------------------
import _stubs  # noqa: E402,F401  (aiosmtplib / aiosmtpd / dotenv stand-ins)

from aiosmtpd.smtp import AuthResult, LoginPassword  # noqa: E402

# Minimal MSAL stand-in that records the apps it is asked to build, so the test
# can assert one app (and therefore one token cache) per app registration.
msal_mod = types.ModuleType("msal")
_MSAL_CREATED = []


class _ConfidentialClientApplication:
    def __init__(self, client_id, authority=None, client_credential=None):
        self.client_id = client_id
        self.authority = authority
        self.client_credential = client_credential
        _MSAL_CREATED.append(self)


msal_mod.ConfidentialClientApplication = _ConfidentialClientApplication
sys.modules["msal"] = msal_mod

# ---------------------------------------------------------------------------
# Test configuration
# ---------------------------------------------------------------------------

TEST_ENV = {
    "AUTH_MODE": "oauth2_graph",
    "LISTEN_PORT": "2525",
    "AZURE_TENANT_ID": "tenant-abc",
    "AZURE_CLIENT_ID": "client-abc",
    "AZURE_CLIENT_SECRET": "secret-abc",
    # account 1 — no suffix
    "O365_USERNAME": "relay1@contoso.com",
    "O365_SEND_AS": "1@some.local",
    "O365_SEND_AS_DOMAINS": "some.local",
    # account 2
    "O365_USERNAME_2": "relay2@contoso.com",
    "O365_PASSWORD_2": "pw2",
    "O365_SEND_AS_2": "2@some.local,printer@some.local",
    # account 5 — sparse numbering must still be discovered
    "O365_USERNAME_5": "relay5@contoso.com",
    # routing / redirects
    "O365_DEFAULT_ACCOUNT": "2",
    "FORWARD_TO_MAP": "1@some.local:person.a@remote.com,some.local:person.b@remote.com",
    "FORWARD_TO": "fallback@remote.com",
    "REWRITE_FROM": "true",
    # allow-lists
    "ALLOWED_SENDERS": "1@some.local,2@some.local,printer@some.local",
    "ALLOWED_SENDER_DOMAINS": "some.local",
    "ALLOWED_CLIENT_IPS": "127.0.0.1",
}

os.environ.update(TEST_ENV)

import relay  # noqa: E402 — must come after the stubs

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


# ---------------------------------------------------------------------------
print("\n[1] account discovery and ordering")
check(
    "accounts 2/1/5 discovered, default first",
    [a.key for a in relay.ACCOUNTS] == ["2", "1", "5"],
    [a.key for a in relay.ACCOUNTS],
)
check("default account flagged", relay.ACCOUNTS[0].is_default is True)

a2, a1, a5 = relay.ACCOUNTS[0], relay.ACCOUNTS[1], relay.ACCOUNTS[2]

# ---------------------------------------------------------------------------
print("\n[2] account selection by sender")
selection_cases = {
    "1@some.local": "relay1@contoso.com",            # explicit send-as
    "someone@some.local": "relay1@contoso.com",      # send-as domain
    "2@some.local": "relay2@contoso.com",            # send-as on account 2
    "printer@some.local": "relay2@contoso.com",
    "relay5@contoso.com": "relay5@contoso.com",      # account's own address
    "nobody@elsewhere.local": "relay2@contoso.com",  # fallback = default account
}
for sender, expected in selection_cases.items():
    got = relay.select_account(sender)
    check(f"{sender} -> {expected}", got is not None and got.email == expected, got and got.email)

# ---------------------------------------------------------------------------
print("\n[3] shared vs per-account credentials")
check("account 1 inherits global tenant", a1.gtenant == "tenant-abc")
check("account 1 inherits global client id", a1.gclient == "client-abc")
check("account 1 inherits global secret", a1.gsecret == "secret-abc")
check("account 2 has its own password", a2.password == "pw2")
check("account 5 inherits global tenant", a5.gtenant == "tenant-abc")
check("one MSAL app shared by same-app accounts", relay._get_msal_app(a1) is relay._get_msal_app(a5))
check("only one MSAL app built for the shared registration", len(_MSAL_CREATED) == 1, len(_MSAL_CREATED))
check(
    "sub-addressed account address still routes (relay5+tag@)",
    relay.select_account("relay5+invoice@contoso.com") is a5,
)

# ---------------------------------------------------------------------------
print("\n[4] per-sender redirect map")
check(
    "exact sender key beats domain key",
    a1.recipient_for("1@some.local") == "person.a@remote.com",
    a1.recipient_for("1@some.local"),
)
check(
    "domain key applies to other senders",
    a2.recipient_for("2@some.local") == "person.b@remote.com",
    a2.recipient_for("2@some.local"),
)
check(
    "global FORWARD_TO is the last resort",
    a2.recipient_for("relay5@contoso.com") == "fallback@remote.com",
    a2.recipient_for("relay5@contoso.com"),
)
check(
    "sub-addressed sender hits the exact map key",
    a1.recipient_for("1+weekly@some.local") == "person.a@remote.com",
    a1.recipient_for("1+weekly@some.local"),
)

# ---------------------------------------------------------------------------
print("\n[5] message preparation")

RAW = (
    b"From: SQL Reports <1@some.local>\r\n"
    b"To: admin@some.local, ops@contoso.com\r\n"
    b"Cc: boss@contoso.com\r\n"
    b"Subject: nightly\r\n"
    b"\r\n"
    b"body\r\n"
)


class _Envelope:
    def __init__(self, mail_from, rcpt_tos, content=RAW):
        self.content = content
        self.mail_from = mail_from
        self.rcpt_tos = rcpt_tos
        self.mail_options = []


msg = relay._prepare_message(_Envelope("1@some.local", ["admin@some.local"]), a1)
check("From rewritten to the routed account", msg["From"] == "relay1@contoso.com", msg["From"])
check("Reply-To keeps the original sender", msg["Reply-To"] == "SQL Reports <1@some.local>", msg["Reply-To"])
check("X-Original-From recorded", msg["X-Original-From"] == "SQL Reports <1@some.local>")
check("X-Relay-Account recorded", msg["X-Relay-Account"] == "relay1@contoso.com")

# ---------------------------------------------------------------------------
print("\n[6] recipient resolution without a redirect")
relay.FORWARD_TO = ""
plain = relay.Account(key="9", email="relay9@contoso.com", send_as=["x@other.local"])
env = _Envelope("x@other.local", ["relay9@contoso.com", "real@contoso.com", "REAL@contoso.com"])
check(
    "relay account removed from recipients, duplicates collapsed",
    relay._recipients_for(env, plain) == ["real@contoso.com"],
    relay._recipients_for(env, plain),
)
relay.FORWARD_TO = "fallback@remote.com"

# ---------------------------------------------------------------------------
print("\n[7] unmatched-sender policy")
relay.UNMATCHED_SENDER_POLICY = "strict"
check("strict rejects unknown senders", relay.select_account("stranger@nowhere.local") is None)
relay.UNMATCHED_SENDER_POLICY = "fallback"
check("fallback uses the default account", relay.select_account("stranger@nowhere.local") is not None)

# ---------------------------------------------------------------------------
print("\n[8] startup validation")
problems = relay._validate_configuration()
check("valid configuration produces no errors", problems == [], problems)

relay.AUTH_MODE = "smtp_auth"
problems = relay._validate_configuration()
check(
    "missing smtp_auth passwords are reported per account",
    any("relay1@contoso.com" in p and "O365_PASSWORD" in p for p in problems),
    problems,
)
relay.AUTH_MODE = "oauth2_graph"

dupe = relay.Account(key="7", email="relay1@contoso.com")
relay.ACCOUNTS.append(dupe)
problems = relay._validate_configuration()
check(
    "duplicate account addresses are rejected",
    any("duplicates" in p for p in problems),
    problems,
)
relay.ACCOUNTS.remove(dupe)

# ---------------------------------------------------------------------------
print("\n[9] relay-side SMTP AUTH")
check("no AUTH configured by default", relay.RELAY_AUTH_ENABLED is False)
check("unknown user rejected", relay._auth_ok("nobody", "whatever") is False)
check("empty username rejected", relay._auth_ok("", "") is False)

relay.RELAY_AUTH_CREDENTIALS = {"sap@some.local": "s3cret"}
relay.RELAY_AUTH_ANY = set()
relay.RELAY_AUTH_ENABLED = True

check("correct credential accepted", relay._auth_ok("sap@some.local", "s3cret") is True)
check("username is case-insensitive", relay._auth_ok("SAP@SOME.LOCAL", "s3cret") is True)
check("wrong password rejected", relay._auth_ok("sap@some.local", "wrong") is False)
check("other sender's password rejected", relay._auth_ok("1@some.local", "s3cret") is False)

relay.RELAY_AUTH_ANY = {"legacy"}
check("any-password user accepted", relay._auth_ok("legacy", "literally-anything") is True)
check("any-password user still needs a username", relay._auth_ok("", "x") is False)

# authenticator integration: wrong credentials must let aiosmtpd send the 535
# itself (handled=False).  handled=True would mean "already replied" and the
# client would hang waiting for a response.
session = types.SimpleNamespace(auth_data=None)
good = relay.smtp_authenticator(None, session, None, "LOGIN", LoginPassword(b"sap@some.local", b"s3cret"))
bad = relay.smtp_authenticator(None, session, None, "LOGIN", LoginPassword(b"sap@some.local", b"nope"))
check("authenticator succeeds on valid login", good.success is True)
check("authenticator fails with handled=False on bad password", bad.success is False and bad.handled is False)

# --- handle_MAIL AUTH gate, for both policies ---------------------------------
# A valid sender AND an allowed peer IP are used, so any rejection can only come
# from the AUTH gate.
handler = relay.RelayHandler()
import asyncio  # noqa: E402

peer = next(iter(relay.ALLOWED_IPS))


def mail_envelope():
    return types.SimpleNamespace(mail_from="", mail_options=[], rcpt_tos=[])


def session_with(authenticated=None, auth_data=None):
    return types.SimpleNamespace(
        peer=(peer, 1234), authenticated=authenticated, auth_data=auth_data
    )


def mail(sess, sender="sap@some.local"):
    return asyncio.run(handler.handle_MAIL(None, sess, mail_envelope(), sender, []))


no_attempt = session_with()                       # client never sent AUTH
ok_plain = session_with(authenticated=True)       # built-in PLAIN/LOGIN success
ok_custom = session_with(auth_data=("x",))        # authenticator-supplied payload
failed = session_with(authenticated=False)        # AUTH attempted, rejected

# default policy: optional -> apps without a password keep working
relay.RELAY_AUTH_POLICY = "optional"
check("optional: no AUTH attempted -> accepted", mail(no_attempt), "250 OK")
check("optional: successful AUTH -> accepted", mail(ok_plain), "250 OK")
check("optional: auth_data only -> accepted", mail(ok_custom), "250 OK")
check("optional: failed AUTH -> refused", mail(failed), "535 5.7.8 Authentication credentials invalid")

# strict policy: everyone must authenticate
relay.RELAY_AUTH_POLICY = "required"
check("required: no AUTH attempted -> 530", mail(no_attempt), "530 5.7.0 Authentication required")
check("required: failed AUTH -> 530", mail(failed), "530 5.7.0 Authentication required")
check("required: successful AUTH -> accepted", mail(ok_plain), "250 OK")
relay.RELAY_AUTH_POLICY = "optional"

# with no relay credentials at all, nothing is gated
relay.RELAY_AUTH_ENABLED = False
check("AUTH disabled: no AUTH attempted -> accepted", mail(no_attempt), "250 OK")
relay.RELAY_AUTH_ENABLED = True

relay.RELAY_AUTH_POLICY = "bogus"
problems = relay._validate_configuration()
check("invalid RELAY_AUTH_POLICY is reported", any("RELAY_AUTH_POLICY" in p for p in problems), True)
relay.RELAY_AUTH_POLICY = "optional"

# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
