"""Read-only, paced portfolio-history import. Never touches AgentDB or an executor."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx

from invo_agent.allowlist import extract_portfolio_ids
from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.research import ResearchArchive, sanitize, utc_now


class HistoryError(RuntimeError):
    pass


def page_items(body: dict, key: str) -> list[dict]:
    if not isinstance(body, dict) or body.get("success") is not True:
        raise HistoryError("History endpoint did not report success")
    items = body.get(key)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise HistoryError(f"History response missing valid {key} list")
    if any(not item.get("id") for item in items):
        raise HistoryError("History record missing identity")
    return items


class HistoryBackfill:
    def __init__(
        self, client: InvolioClient, archive: ResearchArchive, *, delay: float = 1.0,
        max_pages: int = 10000, revisions: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if delay < 0.25 or max_pages < 1:
            raise ValueError("delay must be >= 0.25 seconds and max_pages must be positive")
        self.client, self.archive = client, archive
        self.delay, self.max_pages, self.revisions = delay, max_pages, revisions
        self.sleep = sleep
        self.run_id = str(uuid4())
        self.saved = 0

    def followed_portfolios(self, user_id: str) -> list[str]:
        """Snapshot selection without changing the trader's allowlist or database."""
        ids = set()
        for page in range(1, self.max_pages + 1):
            body = self._request(self.client.get_followed_portfolios,
                                 user_id=user_id, page=page, size=20)
            if (not isinstance(body, dict) or body.get("success") is False
                    or not isinstance(body.get("savedPortfolios"), list)):
                raise HistoryError("Invalid followed-portfolios response")
            self.archive.append("portfolio_selection_page", body, origin="backfill",
                                run_id=self.run_id)
            if not body["savedPortfolios"]:
                return sorted(ids)
            found = extract_portfolio_ids(body)
            if len(found) != len(body["savedPortfolios"]) or not found - ids:
                raise HistoryError("Invalid or repeated followed-portfolios page")
            ids.update(found)
        raise HistoryError("Portfolio selection reached max_pages; selection is incomplete")

    def _request(self, fetch: Callable, **kwargs) -> dict:
        wait = self.delay
        for attempt in range(4):
            self.sleep(wait)
            try:
                return fetch(**kwargs)
            except InvolioAPIError as exc:
                if exc.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise
                wait = max(self.delay, 2 ** (attempt + 1))
                if exc.retry_after:
                    try:
                        retry_seconds = float(exc.retry_after)
                    except ValueError:
                        try:
                            retry_seconds = (parsedate_to_datetime(exc.retry_after)
                                             - datetime.now(UTC)).total_seconds()
                        except (ValueError, TypeError):
                            retry_seconds = 0
                    wait = max(wait, retry_seconds)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                wait = max(self.delay, 2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def _pages(self, fetch: Callable, *, key: str, size: int, scope: dict, **kwargs):
        seen = set()
        for page in range(1, self.max_pages + 1):
            body = self._request(fetch, page=page, size=size, **kwargs)
            received = utc_now()
            # Preserve the full sanitized response, including unmodeled asset classes.
            self.archive.append("history_page", {"scope": scope, "page": page,
                                "response": body}, origin="backfill", run_id=self.run_id,
                                observed_at=received, portfolio_id=scope["portfolio_id"],
                                deduplicate=True)
            items = page_items(body, key)
            if body.get("page") is not None and body["page"] != page:
                raise HistoryError("Server returned a different page than requested")
            signature = hashlib.sha256(json.dumps(
                sorted(str(item["id"]) for item in items)
            ).encode()).hexdigest()
            if items and signature in seen:
                raise HistoryError("Repeated history page; pagination may be unsupported")
            seen.add(signature)
            yield items, received
            # Request until empty: a server-side page-size cap must not truncate history.
            if not items:
                return
        raise HistoryError(f"Reached max_pages={self.max_pages}; import is incomplete")

    def run(self, portfolio_ids: list[str], *, selection: str = "explicit") -> dict:
        portfolios = sorted(set(portfolio_ids))
        if not portfolios:
            raise ValueError("Select at least one portfolio")
        manifest = {"portfolio_ids": portfolios, "selection": selection,
                    "revisions_requested": self.revisions, "max_pages": self.max_pages,
                    "delay_seconds": self.delay}
        self.archive.append("backfill_started", manifest, origin="backfill", run_id=self.run_id)
        failures = []
        try:
            for pid in portfolios:
                bases = set()
                for is_open in (False, True):
                    scope = {"portfolio_id": pid, "is_open": is_open}
                    try:
                        for items, received in self._pages(
                            self.client.get_portfolio_investments, key="investmentsTicker",
                            size=10, scope=scope, portfolio_id=pid, is_open=is_open,
                        ):
                            for item in items:
                                if item.get("baseId"):
                                    bases.add(str(item["baseId"]))
                                else:
                                    # entity_id falls back to the investment id, so
                                    # revisions cannot be grouped: never "complete".
                                    failures.append({**scope, "investment_id": item["id"],
                                                     "error_type": "missing_base_id"})
                                self.saved += self.archive.append(
                                    "investment", item, origin="backfill",
                                    entity_id=str(item.get("baseId") or item["id"]),
                                    portfolio_id=pid, source_at=item.get("createdAt"),
                                    observed_at=received, run_id=self.run_id, deduplicate=True,
                                )
                    except (HistoryError, InvolioAPIError, httpx.HTTPError, ValueError) as exc:
                        if isinstance(exc, InvolioAPIError) and exc.status_code in {401, 403, 429}:
                            raise
                        failures.append({**scope, "error_type": type(exc).__name__})
                if self.revisions:
                    for base in sorted(bases):
                        scope = {"portfolio_id": pid, "base_id": base}
                        try:
                            for items, received in self._pages(
                                self.client.get_investment_history, key="items", size=20,
                                scope=scope, base_id=base,
                            ):
                                for item in items:
                                    self.saved += self.archive.append(
                                        "history_post", item, origin="backfill",
                                        entity_id=base, portfolio_id=pid,
                                        source_at=item.get("createdAt"), observed_at=received,
                                        run_id=self.run_id, deduplicate=True,
                                    )
                        except (HistoryError, InvolioAPIError, httpx.HTTPError, ValueError) as exc:
                            if (isinstance(exc, InvolioAPIError)
                                    and exc.status_code in {401, 403, 429}):
                                raise
                            failures.append({**scope, "error_type": type(exc).__name__})
        except BaseException:
            self.archive.append("backfill_interrupted", {"saved": self.saved},
                                origin="backfill", run_id=self.run_id)
            raise
        result = {"run_id": self.run_id, "saved": self.saved,
                  "status": "incomplete" if failures else "complete", "failures": failures}
        self.archive.append("backfill_finished", sanitize(result),
                            origin="backfill", run_id=self.run_id)
        return result
