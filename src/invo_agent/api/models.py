from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ActionType(StrEnum):
    OPEN = "OPEN"
    UPDATE = "UPDATE"
    CLOSE = "CLOSE"
    IGNORE = "IGNORE"


# Trader/portfolio signals we copy. Own mimic echoes are ignored (noise).
TRADE_NOTIFICATION_TYPES: dict[str, ActionType] = {
    "user_added_investment": ActionType.OPEN,
    "portfolio_added_investment": ActionType.OPEN,
    "user_updated_investment": ActionType.UPDATE,
    "portfolio_updated_investment": ActionType.UPDATE,
    "user_sold_investment": ActionType.CLOSE,
    "portfolio_sold_investment": ActionType.CLOSE,
    "portfolio_investment_auto_closed": ActionType.CLOSE,
    "investment_auto_closed": ActionType.CLOSE,
}


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class UserSnippet(FlexibleModel):
    id: UUID | None = None
    username: str | None = None
    name: str | None = None


class Notification(FlexibleModel):
    id: UUID
    content: str
    notification_type: str = Field(alias="notificationType")
    created_at: datetime = Field(alias="createdAt")
    seen: bool = False
    portfolio_id: UUID | None = Field(default=None, alias="portfolioId")
    post_id: UUID | None = Field(default=None, alias="postId")
    investment_base_id: UUID | None = Field(default=None, alias="investmentBaseId")
    notifier: UserSnippet | None = None
    notifier_id: UUID | None = Field(default=None, alias="notifierId")

    @property
    def action_type(self) -> ActionType:
        return TRADE_NOTIFICATION_TYPES.get(self.notification_type, ActionType.IGNORE)


class NotificationsResponse(FlexibleModel):
    success: bool = False
    items: list[Notification] = Field(default_factory=list)
    page: int | None = None
    size: int | None = None
    error: Any = None


class PortfolioSnippet(FlexibleModel):
    id: UUID | None = None
    title: str | None = None


class InvestmentUpdate(FlexibleModel):
    id: UUID | None = None
    base_id: UUID | None = Field(default=None, alias="baseId")
    base_short_id: str | None = Field(default=None, alias="baseShortId")
    name: str | None = None
    ticker: str | None = None
    direction_long: bool | None = Field(default=None, alias="directionLong")
    leverage: int | float | None = None
    entry_price: float | None = Field(default=None, alias="entryPrice")
    price_target: float | None = Field(default=None, alias="priceTarget")
    stop_loss: float | None = Field(default=None, alias="stopLoss")
    position_size: float | None = Field(default=None, alias="positionSize")
    entry_size: float | None = Field(default=None, alias="entrySize")
    entry_sim: float | None = Field(default=None, alias="entrySim")
    liquidation_price: float | None = Field(default=None, alias="liquidationPrice")
    current_price: float | None = Field(default=None, alias="currentPrice")
    closing_price: float | None = Field(default=None, alias="closingPrice")
    closed_at: datetime | None = Field(default=None, alias="closedAt")
    percent_change: float | None = Field(default=None, alias="percentChange")
    is_open: bool | None = Field(default=None, alias="isOpen")
    active: bool | None = None
    verified_trade: bool | None = Field(default=None, alias="verifiedTrade")
    portfolio: PortfolioSnippet | None = None
    owner: UserSnippet | None = None
    changes: dict[str, Any] | None = None


class Post(FlexibleModel):
    id: UUID | None = None
    post_type_id: str | None = Field(default=None, alias="postTypeId")
    update: InvestmentUpdate | None = None
    owner: UserSnippet | None = None
    content: str | None = None


class PostResponse(FlexibleModel):
    success: bool = False
    post: Post | None = None
    error: Any = None


class OTPLoginResponse(FlexibleModel):
    access_token: str | None = Field(default=None, alias="accessToken")
    refresh_token: str | None = Field(default=None, alias="refreshToken")
    user_id: UUID | None = Field(default=None, alias="userId")
    username: str | None = None
    onboarded: bool | None = None
    error: Any = None


class RefreshTokenResponse(FlexibleModel):
    access_token: str | None = Field(default=None, alias="accessToken")
    refresh_token: str | None = Field(default=None, alias="refreshToken")
    success: bool = False
    error: Any = None


class SuccessErrorResponse(FlexibleModel):
    success: bool = False
    error: Any = None


class TradeAction(FlexibleModel):
    action: ActionType
    notification: Notification
    investment: InvestmentUpdate | None = None
    reason: str | None = None

    @property
    def summary(self) -> str:
        inv = self.investment
        ticker = (inv.ticker or inv.name or "?") if inv else "?"
        side = None
        if inv and inv.direction_long is not None:
            side = "LONG" if inv.direction_long else "SHORT"
        lev = f"{inv.leverage}x" if inv and inv.leverage is not None else "?"
        user = None
        if self.notification.notifier and self.notification.notifier.username:
            user = self.notification.notifier.username
        bits = [self.action.value, ticker]
        if side:
            bits.append(side)
        bits.append(lev)
        if user:
            bits.append(f"@{user}")
        if inv and inv.base_short_id:
            bits.append(inv.base_short_id)
        return " ".join(bits)
