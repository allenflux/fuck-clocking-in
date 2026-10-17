from __future__ import annotations

import http.client
from decimal import Decimal, InvalidOperation
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


PERMANENT_ERRORS = {
    "invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive",
    "missing_scope", "no_permission", "channel_not_found", "not_in_channel",
    "not_allowed_token_type", "restricted_action", "access_denied", "org_login_required",
    "team_access_not_granted", "ekm_access_denied", "app_access_restricted",
}


class SlackError(Exception):
    def __init__(self, method: str, code: str, *, retry_after: float | None = None,
                 ambiguous: bool = False, permanent: bool = False):
        self.method = method
        self.code = code if re.fullmatch(r"[a-zA-Z0-9_]+", code) else "unknown_error"
        self.retry_after = retry_after
        self.ambiguous = ambiguous
        self.permanent = permanent or self.code in PERMANENT_ERRORS
        super().__init__(f"{method}: {self.code}")


def retry_seconds(value: str | None) -> float:
    try:
        number = float(value or "60")
        return max(1.0, number) if math.isfinite(number) else 60.0
    except ValueError:
        return 60.0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SlackClient:
    def __init__(self, token: str, *, timeout: int = 15,
                 stop: threading.Event | None = None, page_interval: int = 60):
        self._token = token
        self.timeout = timeout
        self.stop = stop if stop is not None else threading.Event()
        self.page_interval = page_interval
        self._opener = urllib.request.build_opener(NoRedirect())
        self._history_next = 0.0

    def call(self, method: str, **parameters: Any) -> dict[str, Any]:
        writing = method == "chat.postMessage"
        if writing:
            body = json.dumps(parameters).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            # Use the form encoding accepted by Slack's read endpoints; JSON
            # parameters failed live validation for conversations.info.
            # POST keeps conversation identifiers out of the request URL.
            form = {key: str(value).lower() if isinstance(value, bool) else value
                    for key, value in parameters.items()}
            body = urllib.parse.urlencode(form).encode("utf-8")
            content_type = "application/x-www-form-urlencoded; charset=utf-8"
        request = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=body,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": content_type,
                     "User-Agent": "slack-attendance-responder/1.0"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise SlackError(method, "ratelimited", retry_after=retry_seconds(exc.headers.get("Retry-After"))) from None
            raise SlackError(method, f"http_{exc.code}", ambiguous=writing,
                             permanent=400 <= exc.code < 500) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise SlackError(method, "transport_error", ambiguous=writing) from None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or len(raw) > 2_000_000 or "ok" not in payload:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise SlackError(method, "invalid_response", ambiguous=writing) from None
        if payload["ok"] is not True:
            code = str(payload.get("error", "unknown_error"))
            limited = code in {"ratelimited", "rate_limited"}
            raise SlackError(method, code, retry_after=60 if limited else None,
                             ambiguous=writing and code in {"internal_error", "fatal_error", "request_timeout", "service_unavailable"})
        return payload

    def _history_call(self, **parameters: Any) -> dict[str, Any]:
        delay = max(0.0, self._history_next - time.monotonic())
        if self.stop.wait(delay):
            raise InterruptedError
        self._history_next = time.monotonic() + self.page_interval
        try:
            return self.call("conversations.history", **parameters)
        except SlackError as exc:
            if exc.retry_after is not None:
                self._history_next = max(self._history_next, time.monotonic() + exc.retry_after)
            raise

    @staticmethod
    def _history_page(result: dict[str, Any]) -> tuple[list[dict[str, Any]], str, bool]:
        page = result.get("messages")
        if not isinstance(page, list):
            raise SlackError("conversations.history", "invalid_response")
        for message in page:
            if not isinstance(message, dict) or not isinstance(message.get("ts"), str):
                raise SlackError("conversations.history", "invalid_response")
            try:
                timestamp = Decimal(message["ts"])
                if not timestamp.is_finite() or timestamp < 0:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise SlackError("conversations.history", "invalid_response") from None
        metadata = result.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise SlackError("conversations.history", "invalid_response")
        cursor = metadata.get("next_cursor", "")
        if not isinstance(cursor, str):
            raise SlackError("conversations.history", "invalid_cursor", permanent=True)
        has_more = result.get("has_more", False)
        if not isinstance(has_more, bool):
            raise SlackError("conversations.history", "invalid_response")
        return page, cursor, has_more

    def history(self, channel: str, *, oldest: float) -> list[dict[str, Any]]:
        """Read complete history and refresh its head after backfilling pages."""
        parameters: dict[str, Any] = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 15}
        messages: dict[str, dict[str, Any]] = {}
        seen_cursors: set[str] = set()
        initial_head = parameters["oldest"]
        pages = 0
        while True:
            page, cursor, has_more = self._history_page(self._history_call(**parameters))
            if pages == 0 and page:
                initial_head = max(page, key=lambda message: Decimal(message["ts"]))["ts"]
            pages += 1
            for message in page:
                messages[message["ts"]] = message
            if not cursor:
                if has_more:
                    raise SlackError("conversations.history", "incomplete_history", permanent=True)
                break
            if cursor in seen_cursors:
                raise SlackError("conversations.history", "invalid_cursor", permanent=True)
            seen_cursors.add(cursor)
            parameters["cursor"] = cursor

        if pages > 1:
            # Backfilling may take minutes. Include replies sent since the first
            # page before the caller decides whether a challenge needs a reply.
            page, cursor, has_more = self._history_page(self._history_call(
                channel=channel, oldest=initial_head, inclusive=True, limit=15,
            ))
            if has_more or cursor:
                raise SlackError("conversations.history", "busy_conversation")
            for message in page:
                messages[message["ts"]] = message
        return list(messages.values())

    def post_code(self, channel: str, code: str) -> str:
        result = self.call("chat.postMessage", channel=channel, text=code,
                           mrkdwn=False, parse="none", unfurl_links=False, unfurl_media=False)
        if result.get("channel") != channel or not isinstance(result.get("ts"), str):
            raise SlackError("chat.postMessage", "invalid_response", ambiguous=True)
        return result["ts"]
