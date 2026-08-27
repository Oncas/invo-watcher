from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx

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
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


class InvolioAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class InvolioClient:
    """HTTP client for Involio social (`/v1_0`) and DEX (`/dex`) APIs."""

    def __init__(self, settings: Settings, session: SessionStore):
        self.settings = settings
        self.session = session
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": settings.origin,
                "Referer": f"{settings.origin}/",
                "User-Agent": _BROWSER_UA,
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
        return NotificationsResponse.model_validate(response.json())

    def get_post_by_id(self, post_id: UUID | str) -> PostResponse:
        response = self._request(
            "POST",
            self._social("/posts/get_post_by_id"),
            json={"postId": str(post_id)},
        )
        self._raise_for_status(response, "get post")
        return PostResponse.model_validate(response.json())

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
        return PostResponse.model_validate(response.json())

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
        return response.json()

    # --- dex (raw for now) ---

    def dex_get(self, path: str) -> dict[str, Any]:
        response = self._request("GET", self._dex(path), dex=True)
        self._raise_for_status(response, f"DEX GET {path}")
        return response.json()

    def dex_post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("POST", self._dex(path), json=json or {}, dex=True)
        self._raise_for_status(response, f"DEX POST {path}")
        return response.json()
