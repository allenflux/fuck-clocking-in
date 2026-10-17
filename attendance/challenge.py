"""Strict parsing of the attendance bot's numeric challenges."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Challenge:
    ts: str
    code: str
    deadline: float


def _literal(text: str) -> str:
    """Permit formatting whitespace without relaxing the required wording."""
    return r"\s*".join(re.escape(character) for character in text)


_TEMPLATE = re.compile(
    r"\A\s*"
    + _literal("【考勤抽查】请在")
    + r"\s*(?P<minutes>[0-9]{1,3})\s*"
    + _literal("分钟内(")
    + r"\s*(?P<hour>[0-9]{1,2})\s*:\s*(?P<minute>[0-9]{1,2})\s*"
    + _literal("前)直接回复以下数字:")
    + r"\s*(?P<code>[0-9]{1,16})\s*\Z"
)
_TIMESTAMP = re.compile(r"[0-9]+\.[0-9]+\Z")


def parse_challenge(
    message: Mapping[str, Any], *, now: float, timezone: ZoneInfo
) -> Challenge | None:
    """Return an unexpired challenge only when the entire message matches.

    Both stated limits apply: the earlier of the elapsed-minute deadline and
    the local clock deadline wins. Sender and conversation authorization are
    the caller's responsibility.
    """
    if "subtype" in message and message["subtype"] != "bot_message":
        return None
    if message.get("type", "message") != "message":
        return None
    if "edited" in message or message.get("hidden") is True:
        return None

    text = message.get("text")
    timestamp = message.get("ts")
    if not isinstance(text, str) or not isinstance(timestamp, str):
        return None
    if not _TIMESTAMP.fullmatch(timestamp):
        return None

    match = _TEMPLATE.fullmatch(unicodedata.normalize("NFKC", text))
    if match is None:
        return None

    minutes = int(match["minutes"])
    hour = int(match["hour"])
    minute = int(match["minute"])
    if not 1 <= minutes <= 120 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    try:
        sent = float(Decimal(timestamp))
        if not math.isfinite(sent) or not math.isfinite(now) or sent > now + 30:
            return None
        sent_local = datetime.fromtimestamp(sent, timezone)
        clock_deadline = sent_local.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        elapsed_deadline = sent + minutes * 60
        if clock_deadline < sent_local:
            tomorrow_deadline = clock_deadline + timedelta(days=1)
            # A next-day clock time is plausible only when the stated number
            # of minutes reaches it. Otherwise the displayed deadline already
            # passed, including delayed messages in that same minute.
            if tomorrow_deadline.timestamp() <= elapsed_deadline:
                clock_deadline = tomorrow_deadline
        deadline = min(elapsed_deadline, clock_deadline.timestamp())
    except (InvalidOperation, OverflowError, OSError, ValueError):
        return None

    if now >= deadline:
        return None
    return Challenge(ts=timestamp, code=match["code"], deadline=deadline)
