"""Congress.gov API client and bill-text extraction.

Fetches bill metadata and the latest bill text, stripped to plain
text suitable for an LLM prompt.
"""

from __future__ import annotations

import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Iterator

import requests

CONGRESS_API_BASE = "https://api.congress.gov/v3"
DEFAULT_MAX_CHARS = 600_000  # safety cap for pathologically long bills
REQUEST_TIMEOUT = 30  # seconds

# congress.gov URL slugs for each bill type.
BILL_TYPE_SLUG = {
    "hr": "house-bill",
    "s": "senate-bill",
    "hjres": "house-joint-resolution",
    "sjres": "senate-joint-resolution",
    "hconres": "house-concurrent-resolution",
    "sconres": "senate-concurrent-resolution",
    "hres": "house-resolution",
    "sres": "senate-resolution",
}


def api_get(
    path: str, api_key: str, max_attempts: int = 5, **params: Any
) -> dict[str, Any]:
    """GET a Congress.gov API path with retry on 429/5xx.

    ``path`` is relative to the v3 base, e.g. ``bill/119/hr/2138``.
    """
    url = f"{CONGRESS_API_BASE}/{path.lstrip('/')}"
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(
            url,
            params={"api_key": api_key, "format": "json", **params},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        retryable = resp.status_code == 429 or resp.status_code >= 500
        if not retryable or attempt == max_attempts:
            _raise_for_congress_status(resp)
        wait = float(resp.headers.get("Retry-After") or delay)
        print(
            f"  congress.gov {resp.status_code} on {path}; "
            f"retrying in {wait:.0f}s ({attempt}/{max_attempts})",
            file=sys.stderr,
        )
        time.sleep(wait)
        delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")


def paginate(
    path: str, api_key: str, item_key: str, page_size: int = 250, **params: Any
) -> Iterator[dict[str, Any]]:
    """Yield every item from a paginated Congress.gov list endpoint."""
    offset = 0
    while True:
        data = api_get(
            path, api_key, limit=page_size, offset=offset, **params
        )
        items = data.get(item_key) or []
        yield from items
        offset += len(items)
        count = (data.get("pagination") or {}).get("count", 0)
        if not items or offset >= count:
            return


def fetch_bill(
    congress: int, bill_type: str, number: int, api_key: str
) -> dict[str, Any]:
    """Fetch a single bill's metadata from the Congress.gov API."""
    return api_get(f"bill/{congress}/{bill_type}/{number}", api_key)["bill"]


def fetch_bill_text(
    congress: int,
    bill_type: str,
    number: int,
    api_key: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, dict[str, str]]:
    """Fetch the latest bill text, stripped to plain text.

    Returns ``(text, source)`` where ``source`` describes the version
    used (its ``type`` and the URL the text came from).
    """
    data = api_get(f"bill/{congress}/{bill_type}/{number}/text", api_key)
    versions = data.get("textVersions", [])
    if not versions:
        raise RuntimeError("No text versions available for this bill.")

    version = max(versions, key=lambda v: v.get("date") or "")
    fmt = _pick_format(version.get("formats", []))
    if fmt is None:
        raise RuntimeError(
            "No parseable (Formatted Text / XML) format for the bill."
        )

    doc = requests.get(fmt["url"], timeout=REQUEST_TIMEOUT)
    doc.raise_for_status()
    if fmt["type"] == "Formatted XML":
        text = _xml_to_text(doc.text)
    else:
        text = _html_to_text(doc.text)

    text = text.strip()
    if max_chars and len(text) > max_chars:
        print(
            f"  WARNING: bill text {len(text)} chars exceeds cap "
            f"{max_chars}; truncating.",
            file=sys.stderr,
        )
        text = text[:max_chars]

    return text, {"type": version.get("type", "unknown"), "url": fmt["url"]}


def bill_web_url(congress: int, bill_type: str, number: int) -> str:
    """Human-facing congress.gov URL for the bill."""
    slug = BILL_TYPE_SLUG.get(bill_type.lower(), bill_type.lower())
    ordinal = f"{congress}{ordinal_suffix(congress)}"
    return f"https://www.congress.gov/bill/{ordinal}-congress/{slug}/{number}"


def ordinal_suffix(n: int) -> str:
    """Return the ordinal suffix (st/nd/rd/th) for ``n``."""
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _raise_for_congress_status(resp: requests.Response) -> None:
    """Raise an informative error if a Congress.gov call failed."""
    if resp.status_code != 200:
        raise RuntimeError(
            f"Congress.gov API error {resp.status_code}: {resp.text[:300]}"
        )


def _pick_format(formats: list[dict[str, str]]) -> dict[str, str] | None:
    """Prefer Formatted Text, then Formatted XML; ignore PDF."""
    by_type = {f.get("type"): f for f in formats}
    for preferred in ("Formatted Text", "Formatted XML"):
        if preferred in by_type:
            return by_type[preferred]
    return None


def _html_to_text(raw: str) -> str:
    """Strip a congress.gov HTML bill page down to plain text."""
    match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.IGNORECASE | re.DOTALL)
    body = match.group(1) if match else raw
    body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
    body = re.sub(r"<[^>]+>", "", body)
    return _normalize(html.unescape(body))


def _xml_to_text(raw: str) -> str:
    """Extract readable text from a bill XML document."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _html_to_text(raw)
    return _normalize(" ".join(t for t in root.itertext()))


def _normalize(text: str) -> str:
    """Trim trailing spaces and collapse runs of blank lines."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)
