from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from rich.console import Console

from invo_agent.api.client import InvolioClient
from invo_agent.auth.inbox import InboxError, wait_for_otp
from invo_agent.config import Settings
from invo_agent.dex.turnkey_client import TurnkeyError, TurnkeySigner
from invo_agent.dex.turnkey_crypto import (
    decrypt_credential_bundle,
    generate_p256_keypair,
    public_key_from_private,
)
from invo_agent.events import log_event

console = Console()
logger = logging.getLogger(__name__)


class DexSessionData(BaseModel):
    organization_id: str
    wallet_id: str | None = None
    trading_account_id: str | None = None
    master_address: str
    api_public_key: str
    api_private_key: str
    api_key_id: str | None = None
    credential_type: str | None = None
    expires_at_ms: int | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DexSessionStore:
    def __init__(self, path: Path):
        self.path = path.expanduser()

    def load(self) -> DexSessionData | None:
        if not self.path.exists():
            return None
        try:
            return DexSessionData.model_validate_json(self.path.read_text())
        except Exception:
            return None

    def save(self, data: DexSessionData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = data.model_copy(update={"updated_at": datetime.now(UTC)})
        self.path.write_text(payload.model_dump_json(indent=2))
        self.path.chmod(0o600)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class DexSessionError(RuntimeError):
    pass


def enable_trading_account(client: InvolioClient) -> dict:
    return client.dex_post("/dex/auth/enable", {})


def start_dex_otp(client: InvolioClient) -> tuple[str, int]:
    data = client.dex_post("/dex/auth/otp", {})
    payload = data.get("data") or data
    otp_id = payload.get("otpId")
    ttl = int(payload.get("ttlMs") or 300000)
    if not otp_id:
        raise DexSessionError(f"No otpId in response: {data}")
    return str(otp_id), ttl


# Web clamps ttlDays to 1..30 → seconds. Prefer multi-day overnight sessions.
DEFAULT_DEX_EXPIRATION_SECONDS = 30 * 24 * 60 * 60
# Turnkey default when Involio does not forward expirationSeconds.
TURNKEY_DEFAULT_EXPIRATION_SECONDS = 15 * 60
# Browser-ish label. Turnkey apiKeyName allows only printable ASCII (no "·").
DEFAULT_DEX_DEVICE_LABEL = "Chrome on Mac"


def turnkey_api_key_name(label: str, public_key_hex: str) -> str:
    """Build a Turnkey apiKeyName: alphanumeric + printable ASCII only."""
    suffix = public_key_hex.removeprefix("0x").removeprefix("0X")[-8:]
    raw = f"{label} {suffix}".strip()
    cleaned = "".join(ch for ch in raw if 32 <= ord(ch) <= 126)
    return (cleaned or "invo-agent")[:80]


def complete_dex_otp_login(
    client: InvolioClient,
    *,
    otp_id: str,
    code: str,
    public_key: str,
    expiration_seconds: int = DEFAULT_DEX_EXPIRATION_SECONDS,
    device_label: str = DEFAULT_DEX_DEVICE_LABEL,
) -> dict:
    ttl_days = max(1, min(30, expiration_seconds // 86400))
    body = {
        "otpId": otp_id,
        "code": code.strip(),
        "publicKey": public_key,
        "deviceLabel": device_label,
        "expirationSeconds": expiration_seconds,
        "ttlDays": ttl_days,
    }
    data = client.dex_post("/dex/auth/otp/login", body)
    return data.get("data") or data


def infer_dex_expires_at_ms(
    login: dict, *, now_ms: int | None = None
) -> tuple[int, str]:
    """Pick a stored expiry. Never treat a missing Involio field as 30 days.

    Involio usually omits expiresAtMs and may not forward expirationSeconds to
    Turnkey. Turnkey then issues a 15-minute key. Pretending we got 30 days
    disables background renew and the first trade 401s.
    """
    raw = login.get("expiresAtMs")
    if raw is None:
        raw = login.get("expires_at_ms")
    if raw is not None:
        return int(raw), "involio"
    now_ms = int(datetime.now(UTC).timestamp() * 1000) if now_ms is None else now_ms
    return now_ms + TURNKEY_DEFAULT_EXPIRATION_SECONDS * 1000, "turnkey_default_15m"


def _norm_pub(value: str | None) -> str:
    if not value:
        return ""
    return value.lower().removeprefix("0x")


def refine_expiry_from_turnkey(data: DexSessionData) -> DexSessionData | None:
    """Read the key's real expirationSeconds from Turnkey when possible."""
    try:
        signer = TurnkeySigner(
            organization_id=data.organization_id,
            sign_with_address=data.master_address,
            api_public_key=data.api_public_key,
            api_private_key=data.api_private_key,
        )
        who = signer.whoami()
        user_id = who.get("userId") or (who.get("user") or {}).get("userId")
        if not user_id:
            signer.close()
            return None
        keys = signer.get_api_keys(str(user_id))
        signer.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Turnkey key TTL lookup failed: %s", exc)
        return None
    want = _norm_pub(data.api_public_key)
    for key in keys:
        cred = key.get("credential") or {}
        pub = _norm_pub(str(cred.get("publicKey") or key.get("publicKey") or ""))
        if want:
            if not pub:
                continue
            if pub != want and (len(pub) < 64 or len(want) < 64 or pub[-64:] != want[-64:]):
                continue
        exp_s = key.get("expirationSeconds")
        created = key.get("createdAt") or {}
        created_s = created.get("seconds") if isinstance(created, dict) else None
        if exp_s is None or created_s is None:
            continue
        expires_at_ms = (int(created_s) + int(exp_s)) * 1000
        logger.info(
            "Turnkey API key TTL expirationSeconds=%s created=%s expires_at_ms=%s",
            exp_s,
            created_s,
            expires_at_ms,
        )
        return data.model_copy(update={"expires_at_ms": expires_at_ms})
    return None


def establish_dex_session(
    client: InvolioClient,
    settings: Settings,
    store: DexSessionStore,
    *,
    code: str | None = None,
    code_prompt=None,
) -> DexSessionData:
    """Create/refresh a Turnkey API-key session via Involio DEX email OTP."""
    console.print("[dim]Enabling DEX trading account...[/dim]")
    enabled = enable_trading_account(client)
    en = enabled.get("data") or enabled
    org_id = en.get("organizationId")
    master = en.get("masterAddress") or (en.get("wallet") or {}).get("evmAddress")
    if not org_id or not master:
        raise DexSessionError(f"dex/auth/enable missing org/address: {enabled}")

    keypair = generate_p256_keypair()
    console.print("[dim]Requesting DEX trading OTP...[/dim]")
    started = datetime.now(UTC)
    otp_id, _ttl = start_dex_otp(client)
    console.print("[green]DEX OTP sent to your email.[/green]")

    if not code:
        if settings.imap_configured:
            console.print(
                "[dim]Waiting for Invo Wallet OTP "
                "(from no-reply@mail.turnkey.com)...[/dim]"
            )
            try:
                code = wait_for_otp(
                    settings,
                    kind="wallet",
                    not_before=started,
                    timeout_seconds=90,
                )
                console.print(f"[green]Wallet OTP found:[/green] {code}")
            except InboxError as exc:
                console.print(f"[yellow]IMAP miss:[/yellow] {exc}")
                if code_prompt is None:
                    raise DexSessionError(str(exc)) from exc
                code = code_prompt()
        elif code_prompt:
            code = code_prompt()
        else:
            raise DexSessionError("No DEX OTP code and IMAP not configured")

    if not code:
        raise DexSessionError("Empty DEX OTP code")

    expiration_seconds = DEFAULT_DEX_EXPIRATION_SECONDS
    login = complete_dex_otp_login(
        client,
        otp_id=otp_id,
        code=code,
        public_key=keypair.public_key_otp_hex,
        expiration_seconds=expiration_seconds,
        device_label=DEFAULT_DEX_DEVICE_LABEL,
    )
    bundle = login.get("credentialBundle")
    if not bundle:
        raise DexSessionError(f"No credentialBundle in otp/login: {login}")

    api_private = decrypt_credential_bundle(bundle, keypair.private_key_hex)
    api_public = public_key_from_private(api_private)

    expires_at_ms, ttl_source = infer_dex_expires_at_ms(login)
    session = DexSessionData(
        organization_id=str(org_id),
        wallet_id=en.get("walletId"),
        trading_account_id=en.get("tradingAccountId"),
        master_address=str(master),
        api_public_key=api_public,
        api_private_key=api_private,
        api_key_id=login.get("apiKeyId"),
        credential_type=login.get("credentialType"),
        expires_at_ms=expires_at_ms,
    )
    refined = refine_expiry_from_turnkey(session)
    if refined is not None:
        session = refined
        ttl_source = "turnkey_api_keys"
    remaining_s = session_ttl_remaining_seconds(session)
    logger_msg = (
        f"DEX login TTL requested={expiration_seconds}s "
        f"login_keys={sorted(login.keys())} "
        f"raw_expiresAtMs={login.get('expiresAtMs')} "
        f"source={ttl_source} stored={session.expires_at_ms} "
        f"remaining_s={remaining_s}"
    )
    logger.info("%s", logger_msg)
    console.print(f"[dim]{logger_msg}[/dim]")
    log_event(
        "dex_login_ttl",
        requested_expiration_seconds=expiration_seconds,
        raw_expires_at_ms=login.get("expiresAtMs"),
        stored_expires_at_ms=session.expires_at_ms,
        remaining_seconds=None if remaining_s is None else round(remaining_s, 1),
        ttl_source=ttl_source,
        api_key_id=login.get("apiKeyId"),
    )
    if remaining_s is not None and remaining_s < 3600:
        console.print(
            f"[yellow]DEX key TTL is only {remaining_s / 60:.1f} min "
            f"(requested {expiration_seconds / 86400:.0f}d) — "
            f"will background-renew before it dies.[/yellow]"
        )

    store.save(session)
    try:
        debug_path = Path("captures/dex-session.meta.json")
        debug_path.parent.mkdir(exist_ok=True)
        debug_path.write_text(
            json.dumps(
                {
                    "organization_id": session.organization_id,
                    "master_address": session.master_address,
                    "api_key_id": session.api_key_id,
                    "expires_at_ms": session.expires_at_ms,
                    "ttl_source": ttl_source,
                },
                indent=2,
            )
        )
    except OSError:
        pass
    return session


def session_ttl_remaining_seconds(data: DexSessionData) -> float | None:
    if data.expires_at_ms is None:
        return None
    return (int(data.expires_at_ms) / 1000.0) - datetime.now(UTC).timestamp()


def session_is_expired(data: DexSessionData, *, skew_seconds: int = 60) -> bool:
    remaining = session_ttl_remaining_seconds(data)
    if remaining is None:
        return False
    return remaining <= float(skew_seconds)


def refresh_failed_because_key_dead(exc: BaseException) -> bool:
    """True when API-key refresh cannot work and email OTP is required."""
    if isinstance(exc, TurnkeyError) and exc.status_code in (401, 403):
        return True
    if isinstance(exc, DexSessionError) and "no dex session" in str(exc).lower():
        return True
    msg = str(exc).lower()
    needles = (
        "api_key_expired",
        "expired api key",
        "public_key_not_found",
        "could not find public key",
        "unauthenticated",
        "401",
        "403",
    )
    return any(n in msg for n in needles)


def refresh_dex_session(
    store: DexSessionStore,
    client: InvolioClient | None = None,
    *,
    expiration_seconds: int = DEFAULT_DEX_EXPIRATION_SECONDS,
) -> DexSessionData:
    """Mint a new Turnkey API key using the current still-valid key.

    Matches the Involio website renew path: stamp
    ``ACTIVITY_TYPE_CREATE_READ_WRITE_SESSION_V2`` with ``invalidateExisting=false``.
    No email OTP. When the current key is already dead, Turnkey returns 401/403
    and the caller should fall back to ``establish_dex_session``.
    """
    data, signer = load_signer(store, require_fresh=False)
    try:
        org_id = data.organization_id
        if client is not None:
            try:
                prepared = client.dex_get("/dex/session/rw/prepare")
                payload = prepared.get("data") or prepared
                org = payload.get("organizationId")
                if org:
                    org_id = str(org)
            except Exception as exc:  # noqa: BLE001
                logger.info("DEX rw/prepare skipped: %s", exc)

        keypair = generate_p256_keypair()
        target_pub = keypair.public_key_otp_hex.removeprefix("0x").removeprefix("0X")
        label = turnkey_api_key_name(DEFAULT_DEX_DEVICE_LABEL, target_pub)
        minted = signer.create_read_write_session(
            target_public_key=target_pub,
            api_key_name=label,
            expiration_seconds=expiration_seconds,
            invalidate_existing=False,
        )
        bundle = minted.get("credentialBundle")
        if not bundle:
            raise DexSessionError("Turnkey refresh returned no credentialBundle")
        api_private = decrypt_credential_bundle(str(bundle), keypair.private_key_hex)
        api_public = public_key_from_private(api_private)
        expires_at_ms, ttl_source = infer_dex_expires_at_ms({})
        session = data.model_copy(
            update={
                "organization_id": org_id,
                "api_public_key": api_public,
                "api_private_key": api_private,
                "api_key_id": minted.get("apiKeyId") or None,
                "expires_at_ms": expires_at_ms,
                "updated_at": datetime.now(UTC),
            }
        )
        refined = refine_expiry_from_turnkey(session)
        if refined is not None:
            session = refined
            ttl_source = "turnkey_api_keys"
        remaining_s = session_ttl_remaining_seconds(session)
        store.save(session)
        logger.info(
            "DEX refresh TTL requested=%ss source=%s stored=%s remaining_s=%s api_key_id=%s",
            expiration_seconds,
            ttl_source,
            session.expires_at_ms,
            remaining_s,
            session.api_key_id,
        )
        log_event(
            "dex_refresh",
            ok=True,
            requested_expiration_seconds=expiration_seconds,
            stored_expires_at_ms=session.expires_at_ms,
            remaining_seconds=None if remaining_s is None else round(remaining_s, 1),
            ttl_source=ttl_source,
            api_key_id=session.api_key_id,
        )
        return session
    except TurnkeyError as exc:
        raise DexSessionError(f"DEX API-key refresh failed: {exc}") from exc
    finally:
        signer.close()


def session_should_proactive_renew(
    data: DexSessionData,
    *,
    renew_before_seconds: float = 6 * 3600,
) -> bool:
    """True when we should mint a new key off the trade path (refresh, then OTP).

    Lead time is ``renew_before_seconds``, but never more than 25% of the
    issued TTL (so a 15-minute key renews ~4 minutes before death, not every poll).
    """
    remaining = session_ttl_remaining_seconds(data)
    if remaining is None:
        return False
    if remaining <= 0:
        return True
    issued: float | None = None
    if data.updated_at is not None and data.expires_at_ms is not None:
        issued = (int(data.expires_at_ms) / 1000.0) - data.updated_at.timestamp()
    lead = float(renew_before_seconds)
    if issued is not None and issued > 0:
        lead = min(lead, max(90.0, issued * 0.25))
    else:
        lead = max(90.0, lead)
    return remaining <= lead


def load_signer(
    store: DexSessionStore, *, require_fresh: bool = False
) -> tuple[DexSessionData, TurnkeySigner]:
    data = store.load()
    if data is None:
        raise DexSessionError("No DEX session. Run: invo-agent dex-login")
    if require_fresh and session_is_expired(data):
        raise DexSessionError(
            "DEX session expired. Run: invo-agent dex-login"
        )
    signer = TurnkeySigner(
        organization_id=data.organization_id,
        sign_with_address=data.master_address,
        api_public_key=data.api_public_key,
        api_private_key=data.api_private_key,
    )
    return data, signer
