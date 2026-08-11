from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Literal

from invo_agent.config import Settings

OtpKind = Literal["account", "wallet"]

# Account login: Involio — 4-digit, subject "Verification Code"
# Wallet / DEX: Turnkey — 6-digit, subject "NNNNNN is your sign in code for Invo Wallet"
OTP_PROFILES: dict[OtpKind, dict] = {
    "account": {
        "from_must": ("mail.involio.com", "involio.com"),
        "from_exclude": ("turnkey.com",),
        "subject_hints": ("verification", "verify", "code", "otp"),
        "code_len": 4,
        "label": "Involio account OTP",
    },
    "wallet": {
        "from_must": ("mail.turnkey.com", "turnkey.com"),
        "from_exclude": ("involio.com",),
        "subject_hints": (
            "invo wallet",
            "sign in code",
            "turnkey",
            "verification",
            "code",
            "otp",
        ),
        "code_len": 6,
        "label": "Invo Wallet (Turnkey) OTP",
    },
}

OTP_RE = re.compile(r"\b(\d{4,8})\b")
# Prefer digits that appear first in subject for Turnkey ("228546 is your sign in…")
SUBJECT_LEADING_CODE = re.compile(r"^\s*(\d{4,8})\b")


class InboxError(RuntimeError):
    pass


def _decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _message_text(msg: Message) -> str:
    if msg.is_multipart():
        chunks: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    chunks.append(
                        payload.decode(part.get_content_charset() or "utf-8", "replace")
                    )
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    return str(msg.get_payload())


def extract_otp(text: str, *, prefer_len: int | None = None) -> str | None:
    matches = OTP_RE.findall(text)
    if not matches:
        return None
    if prefer_len is not None:
        for m in matches:
            if len(m) == prefer_len:
                return m
    for m in matches:
        if len(m) == 6:
            return m
    return matches[0]


def _msg_dt(msg: Message) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _matches_profile(sender: str, subject: str, kind: OtpKind) -> bool:
    profile = OTP_PROFILES[kind]
    sender_l = sender.lower()
    subject_l = subject.lower()

    if any(ex in sender_l for ex in profile["from_exclude"]):
        return False
    if not any(must in sender_l for must in profile["from_must"]):
        return False
    # Sender domain is enough when From is exact; subject hints are soft confirm.
    if any(h in subject_l for h in profile["subject_hints"]):
        return True
    # Turnkey / Involio no-reply domains alone are distinctive enough.
    return True


def wait_for_otp(
    settings: Settings,
    *,
    kind: OtpKind = "account",
    timeout_seconds: float = 90.0,
    not_before: datetime | None = None,
) -> str:
    if not settings.imap_configured:
        raise InboxError("IMAP is not configured (IMAP_USER / IMAP_PASSWORD)")
    if kind not in OTP_PROFILES:
        raise InboxError(f"Unknown OTP kind: {kind}")

    user = settings.imap_user or settings.invo_email
    earliest = not_before or (datetime.now(UTC) - timedelta(minutes=2))
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    else:
        earliest = earliest.astimezone(UTC)

    label = OTP_PROFILES[kind]["label"]
    deadline = time.monotonic() + timeout_seconds
    seen_uids: set[bytes] = set()

    while time.monotonic() < deadline:
        code = _poll_once(
            settings,
            user,
            seen_uids,
            earliest=earliest,
            kind=kind,
        )
        if code:
            return code
        time.sleep(2.5)

    raise InboxError(f"No {label} found in Gmail within {timeout_seconds:.0f}s")


def check_imap(settings: Settings) -> str:
    """Login to IMAP and return a short status string."""
    if not settings.imap_configured:
        raise InboxError("IMAP is not configured (set IMAP_USER and IMAP_PASSWORD in .env)")

    user = settings.imap_user or settings.invo_email
    mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        mail.login(user, settings.imap_password)
        typ, _ = mail.select(settings.imap_mailbox)
        if typ != "OK":
            raise InboxError(f"Could not select mailbox {settings.imap_mailbox}")
        typ, data = mail.search(None, "ALL")
        count = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        return f"OK — logged in as {user}, mailbox={settings.imap_mailbox}, messages≈{count}"
    except imaplib.IMAP4.error as exc:
        raise InboxError(
            f"IMAP login failed: {exc}. For Gmail use an App Password (not your normal password)."
        ) from exc
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _poll_once(
    settings: Settings,
    user: str,
    seen_uids: set[bytes],
    *,
    earliest: datetime,
    kind: OtpKind,
) -> str | None:
    profile = OTP_PROFILES[kind]
    prefer_len: int = profile["code_len"]
    mail = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        mail.login(user, settings.imap_password)
        mail.select(settings.imap_mailbox)

        candidates: list[bytes] = []
        for criteria in ("UNSEEN", f'(SINCE "{earliest.strftime("%d-%b-%Y")}")'):
            typ, data = mail.search(None, criteria)
            if typ == "OK" and data and data[0]:
                candidates.extend(data[0].split())

        ordered: list[bytes] = []
        for uid in reversed(candidates):
            if uid not in ordered:
                ordered.append(uid)

        for uid in ordered[:40]:
            if uid in seen_uids:
                continue
            typ, msg_data = mail.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                seen_uids.add(uid)
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                seen_uids.add(uid)
                continue
            msg = email.message_from_bytes(raw)
            msg_time = _msg_dt(msg)
            if msg_time is not None and msg_time < earliest - timedelta(seconds=45):
                seen_uids.add(uid)
                continue

            sender = _decode_mime(msg.get("From"))
            subject = _decode_mime(msg.get("Subject"))
            if not _matches_profile(sender, subject, kind):
                continue

            seen_uids.add(uid)
            # Subject often has the code first (Turnkey wallet).
            leading = SUBJECT_LEADING_CODE.match(subject.strip())
            if leading and len(leading.group(1)) == prefer_len:
                return leading.group(1)

            body = _message_text(msg)
            code = extract_otp(f"{subject}\n{body}", prefer_len=prefer_len)
            if code:
                return code
        return None
    finally:
        try:
            mail.logout()
        except Exception:
            pass
