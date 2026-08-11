from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SessionData(BaseModel):
    email: str
    device_id: str
    access_token: str
    refresh_token: str
    user_id: UUID | None = None
    username: str | None = None
    onboarded: bool | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionStore:
    def __init__(self, path: Path):
        self.path = path.expanduser()

    def ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> SessionData | None:
        if not self.path.exists():
            # Convenience: adopt Phase 0 capture session if present.
            capture = Path("captures/session.json")
            if capture.exists():
                self.ensure_parent()
                shutil.copy2(capture, self.path)
            else:
                return None
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        # Support both snake_case and capture camelCase-ish flat keys.
        if "accessToken" in raw and "access_token" not in raw:
            raw = {
                "email": raw.get("email", ""),
                "device_id": raw.get("deviceId") or raw.get("device_id") or str(uuid4()),
                "access_token": raw["accessToken"],
                "refresh_token": raw.get("refreshToken") or raw.get("refresh_token") or "",
                "user_id": raw.get("userId") or raw.get("user_id"),
                "username": raw.get("username"),
                "onboarded": raw.get("onboarded"),
            }
        return SessionData.model_validate(raw)

    def save(self, data: SessionData) -> None:
        self.ensure_parent()
        payload = data.model_copy(update={"updated_at": datetime.now(UTC)})
        self.path.write_text(payload.model_dump_json(indent=2))
        self.path.chmod(0o600)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def get_or_create_device_id(self) -> str:
        existing = self.load()
        if existing and existing.device_id:
            return existing.device_id
        capture_id = Path("captures/.device_id")
        if capture_id.exists():
            return capture_id.read_text().strip()
        return str(uuid4())
