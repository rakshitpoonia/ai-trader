"""Turning a provider 429 into one readable line for the dashboard log.

A 429 reaches `Trader.run` as whatever the provider's SDK raised, and its useful part - when
the limit resets - is buried in a header, a nested JSON body, or the message text depending
on the provider. Everything here only *reads* that value; the reset time is never computed
from a duration or guessed, because a wrong "try again at" is worse than none.
"""

from datetime import datetime, timezone
import json
import re

MESSAGE = "Model limit exceeded. Try again at {timestamp}."
UNKNOWN = "Model limit exceeded. Try again later; the provider did not say when."

# Providers spell the reset header differently; all of them mean "the limit resets at".
RESET_HEADERS = (
    "x-ratelimit-reset",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "ratelimit-reset",
)

# Epoch values arrive in seconds or milliseconds. Anything past this is milliseconds -
# 1e11 seconds is year 5138, so there is no real overlap between the two ranges.
MILLISECONDS_THRESHOLD = 1e11

ISO_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _format(moment: datetime) -> str:
    """Render a reset time in the reader's own timezone - the dashboard is read by a human.

    The numeric offset rather than %Z: Windows spells that "India Standard Time", which is
    too wide for the log panel, and the offset is unambiguous everywhere.
    """
    local = moment.astimezone()
    return f"{local:%Y-%m-%d %H:%M:%S} (UTC{local:%z})"


def _parse_reset(value) -> str | None:
    """Interpret one candidate reset value: an epoch number, or an ISO-8601 string."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        number = float(text)
    except ValueError:
        pass
    else:
        if number <= 0:
            return None
        if number > MILLISECONDS_THRESHOLD:
            number /= 1000
        try:
            return _format(datetime.fromtimestamp(number, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None

    try:
        return _format(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _body(error: Exception):
    """The parsed JSON body of the failed response, however this provider's SDK exposes it."""
    body = getattr(error, "body", None)
    if isinstance(body, (dict, list)):
        return body
    response = getattr(error, "response", None)
    if response is not None:
        try:
            return response.json()
        except Exception:
            body = getattr(response, "text", None)
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def _headers_in_body(body) -> dict:
    """OpenRouter repeats the rate-limit headers inside error.metadata.headers."""
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    metadata = error.get("metadata") if isinstance(error, dict) else None
    headers = metadata.get("headers") if isinstance(metadata, dict) else None
    return headers if isinstance(headers, dict) else {}


def is_rate_limit(error: Exception) -> bool:
    """Whether this exception is a provider 429 rather than any other failure."""
    if getattr(error, "status_code", None) == 429:
        return True
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    # The Agents SDK sometimes surfaces the provider error only as text, and a researcher
    # 429 arrives having been stringified into a tool result on the way out.
    text = str(error).lower()
    return "429" in text or "rate limit" in text or "quota" in text


def reset_timestamp(error: Exception) -> str | None:
    """The reset time the provider reported, or None if it reported none.

    Read, in order, from: the response headers, the headers echoed inside the JSON body,
    and finally any ISO-8601 timestamp written into the message text. A `Retry-After`
    duration is deliberately ignored - turning a delta into a clock time would be
    calculating the answer rather than reporting it.
    """
    sources = []

    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is not None:
        sources.append({str(k).lower(): v for k, v in dict(headers).items()})

    body = _body(error)
    sources.append({str(k).lower(): v
                    for k, v in _headers_in_body(body).items()})

    for source in sources:
        for header in RESET_HEADERS:
            parsed = _parse_reset(source.get(header))
            if parsed:
                return parsed

    match = ISO_IN_TEXT.search(str(error))
    return _parse_reset(match.group()) if match else None


def rate_limit_message(error: Exception) -> str | None:
    """The dashboard line for a 429, or None if this exception is not one."""
    if not is_rate_limit(error):
        return None
    timestamp = reset_timestamp(error)
    return MESSAGE.format(timestamp=timestamp) if timestamp else UNKNOWN
