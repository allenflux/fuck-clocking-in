"""Extract numeric reply challenges from the authorized attendance bot."""

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
    normal_deadline: float | None = None


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
_TIERED_TEMPLATE = re.compile(
    r"\A\s*"
    + _literal("【考勤抽查】请原样回复以下数字:")
    + r"\s*(?P<opening>【|\[)\s*(?P<code>[0-9]{1,16})\s*(?P<closing>】|\])\s*"
    + r"[·•]\s*(?P<normal_minutes>[0-9]{1,3})\s*"
    + _literal("分钟内(")
    + r"\s*(?P<normal_hour>[0-9]{1,2})\s*:\s*(?P<normal_minute>[0-9]{1,2})\s*"
    + _literal("前)回复→正常")
    + r"\s*[·•]\s*(?P<range_start>[0-9]{1,3})\s*[—–-]\s*(?P<range_end>[0-9]{1,3})\s*"
    + _literal("分钟内回复→超时回复")
    + r"\s*[·•]\s*"
    + _literal("超过")
    + r"\s*(?P<hard_minutes>[0-9]{1,3})\s*"
    + _literal("分钟(")
    + r"\s*(?P<hard_hour>[0-9]{1,2})\s*:\s*(?P<hard_minute>[0-9]{1,2})\s*"
    + _literal(")未回复→未响应")
    + r"\s*\Z"
)
_TIMESTAMP = re.compile(r"[0-9]+\.[0-9]+\Z")
_ACKNOWLEDGMENT = re.compile(
    r"(?:考勤|抽查|验证|校验|签到|打卡|到岗)(?:抽查)?\s*(?:已)?\s*(?:通过|成功|完成|正确)"
    r"|已\s*(?:收到|接收|记录|完成|回复)|感谢\s*(?:配合|回复)"
    r"|(?:回复|回答|答复|数字)\s*(?:正确|成功|已通过)|匹配\s*成功"
    r"|\b(?:thank(?:s|\s+you)|received|acknowledged|(?:check|verification|attendance)\s+(?:passed|successful|complete))\b",
    re.IGNORECASE,
)
_ATTENDANCE = re.compile(r"考勤|抽查|签到|到岗|出勤|\battendance\b|\bcheck[ -]?in\b", re.IGNORECASE)
_REQUEST_WORDS = r"回复|回覆|发送|输入|提交|\breply\b|\brespond\b|\bsend\b|\benter\b|\bsubmit\b"
_CODE_WORDS = r"验证码|校验码|考勤码|签到码|随机码|数字|口令|\bcode\b|\bdigits?\b|\bnumber\b"
_REQUEST = re.compile(_REQUEST_WORDS, re.IGNORECASE)
_IMPERATIVE = re.compile(
    r"(?:请|务必|需要|必须|直接|原样|立即)[^0-9\n]{0,16}(?:回复|回覆|发送|输入|提交)"
    r"|\b(?:reply|respond|send|enter|submit)\b",
    re.IGNORECASE,
)
_LABELLED_NUMBER = re.compile(
    rf"(?:{_CODE_WORDS}|{_REQUEST_WORDS})[^0-9]{{0,48}}(?P<code>[0-9]+)",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"[0-9]+")
_CLOCK = re.compile(r"(?<![0-9])(?P<hour>[0-9]{1,2})\s*:\s*(?P<minute>[0-9]{1,2})(?![0-9])")
_DURATION = re.compile(
    r"(?<![0-9])(?:(?P<start>[0-9]{1,3})\s*[—–\-~至到]\s*)?"
    r"(?P<minutes>[0-9]+)\s*(?:分钟|分鍾|minutes?\b|mins?\b)",
    re.IGNORECASE,
)
_NON_CODE_NUMBER = re.compile(
    r"[0-9]+(?:\s*:\s*[0-9]+){1,2}"
    r"|[0-9]{4}\s*[-/]\s*[0-9]{1,2}\s*[-/]\s*[0-9]{1,2}"
    r"|(?:[0-9]{4}\s*年\s*)?[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*[日号]"
    r"|[0-9]+(?:\s*[—–\-~至到]\s*[0-9]+)?\s*(?:分钟|分鍾|小时|秒|天|人|次|条|个|份|minutes?\b|mins?\b|hours?\b|hrs?\b|seconds?\b|secs?\b)"
    r"|[0-9]+\s*%"
    r"|https?://[^\s<>]+|<[@#!][^>]+>",
    re.IGNORECASE,
)
_PLAIN_CODE = re.compile(r"\s*(?:[【\[「『`\"']\s*)?[0-9]{1,16}(?:\s*[】\]」』`\"'])?\s*\Z")
_WRAPPED_CODE = re.compile(r"[【\[「『`\"']\s*(?P<code>[0-9]+)\s*[】\]」』`\"']")


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("*", "").replace("_", "")


def looks_like_challenge(text: str) -> bool:
    """Recognize prompts for diagnostics while ignoring success acknowledgments."""
    normalized = _normalized(text)
    acknowledgment = _ACKNOWLEDGMENT.search(normalized)
    if acknowledgment is not None:
        # Prompts may describe the later success notification in a footer.
        # A genuine acknowledgment has no earlier instruction and code.
        earlier = normalized[:acknowledgment.start()]
        if not (_IMPERATIVE.search(earlier) and _LABELLED_NUMBER.search(earlier)):
            return False
    return bool(
        _ATTENDANCE.search(normalized)
        or _REQUEST.search(normalized)
        or _PLAIN_CODE.fullmatch(normalized)
    )


def _extract_code(text: str) -> str | None:
    blocked = [match.span() for match in _NON_CODE_NUMBER.finditer(text)]
    numbers = list(_NUMBER.finditer(text))

    def is_blocked(match: re.Match[str]) -> bool:
        return any(start <= match.start() and match.end() <= end for start, end in blocked)

    valid = {}
    for index, match in enumerate(numbers):
        start, end = match.span()
        if is_blocked(match) or len(match[0]) > 16:
            continue
        before, after = text[:start], text[end:]
        if re.search(r"[A-Za-z0-9_+\-−/]\Z", before) or re.match(r"[A-Za-z0-9_/]", after):
            continue
        # Whitespace-separated digits such as "1 362" are not one code.
        neighbors = numbers[max(0, index - 1):index] + numbers[index + 1:index + 2]
        if any(
            not is_blocked(other)
            and re.fullmatch(r"[ \t]+|[.,][ \t]*", text[min(end, other.end()):max(start, other.start())])
            for other in neighbors
        ):
            continue
        valid[(start, end)] = match[0]

    candidates = set(valid.values())
    if len(candidates) != 1:
        return None
    labelled = {
        valid[match.span("code")]
        for match in _LABELLED_NUMBER.finditer(text)
        if match.span("code") in valid
    }
    if labelled:
        return labelled.pop() if len(labelled) == 1 else None
    wrapped = {
        valid[match.span("code")]
        for match in _WRAPPED_CODE.finditer(text)
        if match.span("code") in valid
    }
    if wrapped:
        return wrapped.pop() if len(wrapped) == 1 else None
    return candidates.pop() if len(candidates) == 1 else None


def _generic_deadline(text: str, *, sent: float, sent_local: datetime) -> float | None:
    durations = list(_DURATION.finditer(text))
    values = [int(match["minutes"]) for match in durations]
    if any(not 1 <= value <= 120 for value in values):
        return None
    if any(match["start"] is not None and not 0 < int(match["start"]) < int(match["minutes"]) for match in durations):
        return None
    minutes = max(values, default=30)
    deadline = sent + minutes * 60
    for clock in _CLOCK.finditer(text):
        associated = next(
            (duration for duration in reversed(durations)
             if duration.end() <= clock.start()
             and re.fullmatch(r"\s*(?:内|以内|后)?\s*[,;:]?\s*\(?\s*", text[duration.end():clock.start()])),
            None,
        )
        if associated is None:
            associated = next(
                (duration for duration in durations
                 if duration.start() >= clock.end()
                 and re.fullmatch(r"\s*(?:前|之前)?\s*\(?\s*", text[clock.end():duration.start()])),
                None,
            )
        prefix, suffix = text[max(0, clock.start() - 24):clock.start()], text[clock.end():clock.end() + 8]
        explicit = bool(
            re.search(r"(?:截止(?:时间)?|最晚|最后期限|到期|有效至|\bexpires?\s*(?:at)?|\bby|\bbefore)\s*:?\s*\(?\s*\Z", prefix, re.IGNORECASE)
            or re.match(r"\s*(?:前|之前)", suffix)
        )
        if associated is not None:
            if int(associated["minutes"]) != minutes:
                continue  # A normal cutoff is not the final reply cutoff.
            explicit = True
        if not explicit:
            continue
        hour, minute = int(clock["hour"]), int(clock["minute"])
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        deadline = min(deadline, _clock_deadline(sent_local, minutes=minutes, hour=hour, minute=minute))
    return deadline


def _clock_deadline(sent_local: datetime, *, minutes: int, hour: int, minute: int) -> float:
    clock = sent_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if clock < sent_local:
        tomorrow = clock + timedelta(days=1)
        # Roll over only when the stated elapsed limit can reach tomorrow.
        if tomorrow.timestamp() <= sent_local.timestamp() + minutes * 60:
            clock = tomorrow
    return clock.timestamp()


def _parse_generic(text: str, timestamp: str, *, now: float, timezone: ZoneInfo) -> Challenge | None:
    text = _normalized(text)
    if not looks_like_challenge(text):
        return None
    code = _extract_code(text)
    if code is None:
        return None
    try:
        sent = float(Decimal(timestamp))
        if not math.isfinite(sent) or not math.isfinite(now) or sent > now + 30:
            return None
        sent_local = datetime.fromtimestamp(sent, timezone)
        deadline = _generic_deadline(text, sent=sent, sent_local=sent_local)
    except (InvalidOperation, OverflowError, OSError, ValueError):
        return None
    if deadline is None or now >= deadline:
        return None
    return Challenge(ts=timestamp, code=code, deadline=deadline)


def parse_challenge(
    message: Mapping[str, Any], *, now: float, timezone: ZoneInfo
) -> Challenge | None:
    """Return an unexpired numeric challenge despite changes to the bot's wording.

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

    normalized = unicodedata.normalize("NFKC", text)
    match = _TEMPLATE.fullmatch(normalized)
    tiered = False
    if match is None:
        match = _TIERED_TEMPLATE.fullmatch(normalized)
        tiered = True
    if match is None:
        return _parse_generic(text, timestamp, now=now, timezone=timezone)

    minutes = int(match["hard_minutes"] if tiered else match["minutes"])
    hour = int(match["hard_hour"] if tiered else match["hour"])
    minute = int(match["hard_minute"] if tiered else match["minute"])
    if not 1 <= minutes <= 120 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None

    if tiered:
        normal_minutes = int(match["normal_minutes"])
        normal_hour = int(match["normal_hour"])
        normal_minute = int(match["normal_minute"])
        if (
            not 0 < normal_minutes < minutes
            or int(match["range_start"]) != normal_minutes
            or int(match["range_end"]) != minutes
            or not 0 <= normal_hour <= 23
            or not 0 <= normal_minute <= 59
            or (match["opening"], match["closing"]) not in (("【", "】"), ("[", "]"))
        ):
            return None

    normal_deadline = None
    try:
        sent = float(Decimal(timestamp))
        if not math.isfinite(sent) or not math.isfinite(now) or sent > now + 30:
            return None
        sent_local = datetime.fromtimestamp(sent, timezone)
        clock_deadline = _clock_deadline(
            sent_local, minutes=minutes, hour=hour, minute=minute
        )
        elapsed_deadline = sent + minutes * 60
        deadline = min(elapsed_deadline, clock_deadline)
        if tiered:
            normal_clock_deadline = _clock_deadline(
                sent_local,
                minutes=normal_minutes,
                hour=normal_hour,
                minute=normal_minute,
            )
            if (
                normal_clock_deadline <= sent
                or clock_deadline - normal_clock_deadline != (minutes - normal_minutes) * 60
            ):
                return None
            normal_deadline = min(sent + normal_minutes * 60, normal_clock_deadline)
    except (InvalidOperation, OverflowError, OSError, ValueError):
        return None

    if now >= deadline:
        return None
    return Challenge(
        ts=timestamp,
        code=match["code"],
        deadline=deadline,
        normal_deadline=normal_deadline,
    )
