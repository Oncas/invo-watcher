from __future__ import annotations

import json as jsonlib
from typing import Any
from uuid import UUID, uuid4

import httpx

from invo_agent import __version__
from invo_agent.api.models import (
    NotificationsResponse,
    OTPLoginResponse,
    PostResponse,
    RefreshTokenResponse,
    SuccessErrorResponse,
)
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings

# Match Flutter web Dio interceptor (app.invoapp.com → version.json).
_WEB_APP_VERSION = "0.0.85"
_WEB_APP_RELEASE = "1.0.59"
_WEB_APP_BUILD = "83"
_WEB_PLATFORM = "web"
_DEX_CLIENT_VERSION = "involio-dex/fe"
# Say what this is: the x-app-* headers above keep API compatibility, but the
# client never claims to be a browser.
_USER_AGENT = f"invo-watcher/{__version__} (+https://github.com/Oncas/invo-watcher)"


class InvolioAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


_ERROR_DETAIL_MAX = 1000


def api_error_detail(exc: BaseException) -> str:
    """Render an exception together with whatever the server sent back.

    ``str(exc)`` on an :class:`InvolioAPIError` is only the status line, which
    says a call was rejected but never why. The body holds the reason, so log
    sites that record a failure use this instead.
    """
    message = str(exc)
    body = getattr(exc, "body", None)
    if body is None or body == "":
        return message
    if isinstance(body, str):
        rendered = body
    else:
        try:
            rendered = jsonlib.dumps(body, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = repr(body)
    detail = f"{message}: {rendered}"
    if len(detail) > _ERROR_DETAIL_MAX:
        detail = detail[: _ERROR_DETAIL_MAX - 1] + "…"
    return detail


class InvolioClient:
    """HTTP client for Involio social (`/v1_0`) and DEX (`/dex`) APIs."""

    def __init__(self, settings: Settings, session: SessionStore):
        self.settings = settings
        self.session = session
        # Installed only by the trading CLI; never observes authentication requests.
        self.research = None
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": settings.origin,
                "Referer": f"{settings.origin}/",
                "User-Agent": _USER_AGENT,
                "x-app-version": _WEB_APP_VERSION,
                "x-app-release": _WEB_APP_RELEASE,
                "x-app-build-number": _WEB_APP_BUILD,
                "x-platform": _WEB_PLATFORM,
            },
        )

    def _browser_headers(self, *, dex: bool = False) -> dict[str, str]:
        headers = {"x-request-id": str(uuid4())}
        if dex:
            headers["X-Client-Version"] = _DEX_CLIENT_VERSION
        return headers

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> InvolioClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _auth_headers(self, *, use_refresh: bool = False) -> dict[str, str]:
        data = self.session.load()
        if not data:
            raise InvolioAPIError("Not logged in. Run: invo-agent login")
        token = data.refresh_token if use_refresh else data.access_token
        if not token:
            raise InvolioAPIError("Session missing token. Run: invo-agent login")
        return {"Authorization": f"Bearer {token}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        use_refresh: bool = False,
        retry_on_401: bool = True,
        dex: bool = False,
    ) -> httpx.Response:
        headers = self._browser_headers(dex=dex)
        if auth:
            headers.update(self._auth_headers(use_refresh=use_refresh))
        response = self._client.request(method, url, json=json, headers=headers)

        if (
            auth
            and not use_refresh
            and retry_on_401
            and response.status_code == 401
            and self.session.load() is not None
        ):
            self.refresh_tokens()
            headers = self._browser_headers(dex=dex)
            headers.update(self._auth_headers())
            response = self._client.request(method, url, json=json, headers=headers)

        return response

    def _social(self, path: str) -> str:
        return f"{self.settings.social_api_base.rstrip('/')}/{path.lstrip('/')}"

    def _dex(self, path: str) -> str:
        return f"{self.settings.dex_api_base.rstrip('/')}/{path.lstrip('/')}"

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = response.text
        hint = ""
        if response.status_code == 429:
            hint = " — rate limited; wait a minute and retry"
        raise InvolioAPIError(
            f"{action} failed ({response.status_code}){hint}",
            status_code=response.status_code,
            body=body,
            retry_after=response.headers.get("Retry-After"),
        )

    # --- auth ---

    def login_email_start(self, email: str) -> SuccessErrorResponse:
        response = self._request(
            "POST",
            self._social("/auth/login/email/start"),
            json={"email": email},
            auth=False,
        )
        self._raise_for_status(response, "login start")
        return SuccessErrorResponse.model_validate(response.json())

    def login_email(
        self,
        *,
        email: str,
        code: str,
        device_id: str,
        device_type: str | None = None,
        device_info: str | None = None,
    ) -> OTPLoginResponse:
        # Flutter web sends deviceType/deviceInfo as null.
        payload = {
            "email": email,
            "code": code,
            "deviceId": device_id,
            "deviceType": device_type,
            "deviceInfo": device_info,
        }
        response = self._request(
            "POST",
            self._social("/auth/login/email"),
            json=payload,
            auth=False,
        )
        self._raise_for_status(response, "login")
        return OTPLoginResponse.model_validate(response.json())

    def refresh_tokens(self) -> RefreshTokenResponse:
        response = self._request(
            "GET",
            self._social("/auth/refresh_token"),
            auth=True,
            use_refresh=True,
            retry_on_401=False,
        )
        self._raise_for_status(response, "refresh token")
        data = RefreshTokenResponse.model_validate(response.json())
        if not data.success or not data.access_token or not data.refresh_token:
            raise InvolioAPIError("Refresh token rejected", body=data.model_dump())
        current = self.session.load()
        if current is None:
            raise InvolioAPIError("No session to refresh")
        self.session.save(
            current.model_copy(
                update={
                    "access_token": data.access_token,
                    "refresh_token": data.refresh_token,
                }
            )
        )
        return data

    # --- notifications / posts ---

    def unseen_count(self) -> int:
        response = self._request("GET", self._social("/notifications/unseen_count"))
        self._raise_for_status(response, "unseen count")
        body = response.json()
        return int(body.get("count", 0))

    def get_notifications(self, *, page: int = 1, size: int | None = None) -> NotificationsResponse:
        size = size if size is not None else self.settings.notification_page_size
        response = self._request(
            "POST",
            self._social("/notifications/get_notifications"),
            json={"page": page, "size": size},
        )
        self._raise_for_status(response, "get notifications")
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("items"), list):
            for item in body["items"]:
                self._observe("notification", item)
        return NotificationsResponse.model_validate(body)

    def get_post_by_id(self, post_id: UUID | str) -> PostResponse:
        response = self._request(
            "POST",
            self._social("/posts/get_post_by_id"),
            json={"postId": str(post_id)},
        )
        self._raise_for_status(response, "get post")
        body = response.json()
        self._observe("post", body.get("post"))
        return PostResponse.model_validate(body)

    def get_current_shadow_post_by_investment_base_id(
        self, base_id: UUID | str
    ) -> PostResponse:
        """Latest shadow post for an investment base id (open or closed)."""
        response = self._request(
            "POST",
            self._social("/posts/get_current_shadow_post_by_investment_base_id"),
            json={"baseId": str(base_id)},
        )
        self._raise_for_status(response, "get shadow post by investment base id")
        body = response.json()
        self._observe("latest_post", body.get("post"), entity_id=str(base_id))
        return PostResponse.model_validate(body)

    def _observe(self, kind: str, payload: Any, *, entity_id: str | None = None) -> None:
        if self.research is None or not isinstance(payload, dict):
            return
        try:
            update = payload.get("update")
            update = update if isinstance(update, dict) else {}
            portfolio = update.get("portfolio")
            portfolio_id = payload.get("portfolioId")
            if portfolio_id is None and isinstance(portfolio, dict):
                portfolio_id = portfolio.get("id")
            # Key posts by the investment base id, like the backfill does, so
            # entity_id joins across live and historical records. A post's own
            # id is not a join key for anything else we store.
            identity = (
                entity_id or update.get("baseId")
                or payload.get("baseId") or payload.get("id")
            )
            self.research.record(
                kind, payload, entity_id=None if identity is None else str(identity),
                portfolio_id=portfolio_id,
                source_at=payload.get("createdAt"), deduplicate=True,
            )
        except Exception:
            # Research must not change execution behavior, including on malformed data.
            pass

    def get_portfolio_investments(
        self, portfolio_id: UUID | str, *, is_open: bool, page: int = 1, size: int = 10,
    ) -> dict[str, Any]:
        """Contract verified against the public Involio web app (2026-09-05)."""
        response = self._request(
            "POST", self._social("/investments/get_investments"),
            json={"portfolioId": str(portfolio_id), "isOpen": is_open,
                  "params": {"page": page, "size": size}},
        )
        self._raise_for_status(response, "portfolio investments")
        return response.json()

    def get_investment_history(
        self, base_id: UUID | str, *, page: int = 1, size: int = 20,
    ) -> dict[str, Any]:
        """Available investment posts; these may themselves have been edited."""
        response = self._request(
            "POST", self._social("/posts/get_investment_history"),
            json={"investmentBaseId": str(base_id), "params": {
                "page": page, "size": size, "shadowInvestmentsOnly": False,
            }},
        )
        self._raise_for_status(response, "investment history")
        return response.json()

    def get_followed_portfolios(
        self, user_id: UUID | str, *, page: int = 1, size: int = 50
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            self._social("/portfolios/get_users_followed_portfolios"),
            json={
                "context": {"filterType": "all"},
                "userId": str(user_id),
                "params": {"page": page, "size": size},
            },
        )
        self._raise_for_status(response, "followed portfolios")
        body = response.json()
        self._observe("followed_portfolios", body)
        return body

    # --- dex (raw for now) ---

    def dex_get(self, path: str) -> dict[str, Any]:
        response = self._request("GET", self._dex(path), dex=True)
        self._raise_for_status(response, f"DEX GET {path}")
        return response.json()

    def dex_post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("POST", self._dex(path), json=json or {}, dex=True)
        self._raise_for_status(response, f"DEX POST {path}")
        return response.json()
