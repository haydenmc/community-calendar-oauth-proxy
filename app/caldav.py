"""Server-side CalDAV client used by the web viewer and by startup bootstrap."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import anyio
import httpx

from .config import Settings

log = logging.getLogger(__name__)

# The calendar backend may still be starting when we are. Compose orders startup
# with a healthcheck, but nothing guarantees that elsewhere, so retry.
BOOTSTRAP_ATTEMPTS = 15
BOOTSTRAP_DELAY_SECONDS = 2.0

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CALSERVER_NS = "http://calendarserver.org/ns/"

# The collection's ctag changes whenever anything inside it does, so fetching it
# is a cheap way to ask "is what I cached still current?".
PROPFIND_CTAG = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:CS="http://calendarserver.org/ns/">
  <D:prop><CS:getctag/></D:prop>
</D:propfind>
"""

# The time-range filter is what keeps a page view proportional to the window on
# screen rather than to the whole calendar. Per RFC 4791 the server matches a
# recurring event when any of its occurrences falls in the range, so events with
# an RRULE that started years ago still come back; what it returns is the whole
# document, RRULE intact, which expand_events then expands client-side.
CALENDAR_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""

# iCalendar UTC form, e.g. 20260301T000000Z (RFC 5545 4.3.5).
CALDAV_TIME_FORMAT = "%Y%m%dT%H%M%SZ"


def format_caldav_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime(CALDAV_TIME_FORMAT)

MKCALENDAR_BODY = """<?xml version="1.0" encoding="utf-8"?>
<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:set>
    <D:prop>
      <D:displayname>{display_name}</D:displayname>
      <C:supported-calendar-component-set>
        <C:comp name="VEVENT"/>
        <C:comp name="VTODO"/>
      </C:supported-calendar-component-set>
    </D:prop>
  </D:set>
</C:mkcalendar>
"""

PROPFIND_EXISTS = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/><D:displayname/></D:prop></D:propfind>
"""

PROPPATCH_DISPLAYNAME = """<?xml version="1.0" encoding="utf-8"?>
<D:propertyupdate xmlns:D="DAV:">
  <D:set><D:prop><D:displayname>{display_name}</D:displayname></D:prop></D:set>
</D:propertyupdate>
"""


def _admin_headers(settings: Settings, **extra: str) -> dict[str, str]:
    headers = {"X-Remote-User": settings.shared_principal, "Content-Type": "application/xml; charset=utf-8"}
    headers.update(extra)
    return headers


def parse_display_name(xml_body: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError:
        return None
    for el in root.iter(f"{{{DAV_NS}}}displayname"):
        return el.text or ""
    return None


async def ensure_collection(client: httpx.AsyncClient, settings: Settings) -> None:
    """Create the shared calendar collection, or rename it if the title changed."""
    url = settings.radicale_url.rstrip("/") + settings.shared_path
    probe = await client.request(
        "PROPFIND", url, headers=_admin_headers(settings, Depth="0"), content=PROPFIND_EXISTS
    )
    if probe.status_code in (207, 200):
        current = parse_display_name(probe.content)
        if current is not None and current != settings.shared_display_name:
            # SHARED_DISPLAY_NAME is only applied at creation otherwise, so a
            # later change would silently do nothing.
            renamed = await client.request(
                "PROPPATCH",
                url,
                headers=_admin_headers(settings),
                content=PROPPATCH_DISPLAYNAME.format(
                    display_name=escape(settings.shared_display_name)
                ),
            )
            if renamed.status_code in (207, 200):
                log.info("renamed shared calendar to %r", settings.shared_display_name)
            else:
                log.warning("could not rename shared calendar: %s", renamed.status_code)
        else:
            log.info("shared calendar already present at %s", settings.shared_path)
        return
    if probe.status_code not in (403, 404, 409):
        log.warning("unexpected status %s probing shared calendar", probe.status_code)

    body = MKCALENDAR_BODY.format(display_name=escape(settings.shared_display_name))
    created = await client.request("MKCALENDAR", url, headers=_admin_headers(settings), content=body)
    if created.status_code == 409:
        # Parent principal collection missing - create it, then retry.
        parent = settings.radicale_url.rstrip("/") + f"/{settings.shared_principal}/"
        await client.request("MKCOL", parent, headers=_admin_headers(settings))
        created = await client.request("MKCALENDAR", url, headers=_admin_headers(settings), content=body)

    if created.status_code in (201, 200):
        log.info("created shared calendar at %s", settings.shared_path)
    else:
        log.error(
            "could not create shared calendar at %s: %s %s",
            settings.shared_path,
            created.status_code,
            created.text[:200],
        )


async def ensure_collection_with_retry(
    client: httpx.AsyncClient,
    settings: Settings,
    attempts: int = BOOTSTRAP_ATTEMPTS,
    delay: float = BOOTSTRAP_DELAY_SECONDS,
) -> bool:
    """Bootstrap the collection, waiting for the backend to come up."""
    for attempt in range(1, attempts + 1):
        try:
            await ensure_collection(client, settings)
        except httpx.HTTPError as exc:
            if attempt == attempts:
                log.error("calendar backend unreachable after %s attempts: %s", attempts, exc)
                return False
            log.warning(
                "calendar backend not ready (attempt %s/%s), retrying in %ss: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            await anyio.sleep(delay)
        else:
            return True
    return False


def parse_calendar_data(xml_body: bytes) -> list[str]:
    """Pull the calendar-data payloads out of a CalDAV multistatus response."""
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError as exc:
        log.error("malformed multistatus response from backend: %s", exc)
        return []
    return [
        el.text
        for el in root.iter(f"{{{CALDAV_NS}}}calendar-data")
        if el.text and el.text.strip()
    ]


def parse_ctag(xml_body: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError:
        return None
    for el in root.iter(f"{{{CALSERVER_NS}}}getctag"):
        if el.text:
            return el.text
    return None


async def fetch_ctag(client: httpx.AsyncClient, settings: Settings) -> str | None:
    """The collection's change tag, or None if it cannot be determined."""
    url = settings.radicale_url.rstrip("/") + settings.shared_path
    try:
        response = await client.request(
            "PROPFIND", url, headers=_admin_headers(settings, Depth="0"), content=PROPFIND_CTAG
        )
    except httpx.HTTPError as exc:
        log.warning("could not read the calendar ctag: %s", exc)
        return None
    if response.status_code != 207:
        log.warning("ctag PROPFIND returned %s", response.status_code)
        return None
    return parse_ctag(response.content)


class CalendarCache:
    """Documents per window, valid only while the collection's ctag holds.

    Lives on the event loop and is only touched between awaits, so it needs no
    lock; two concurrent misses simply both fetch, which costs a duplicate
    request and nothing else.
    """

    def __init__(self, max_windows: int = 24) -> None:
        self._max_windows = max_windows
        self._ctag: str | None = None
        # Insertion-ordered, so the oldest window is the one evicted.
        self._windows: dict[tuple[str, str], list[str]] = {}

    def get(self, ctag: str | None, window: tuple[str, str]) -> list[str] | None:
        if ctag is None or ctag != self._ctag:
            return None
        return self._windows.get(window)

    def put(self, ctag: str | None, window: tuple[str, str], documents: list[str]) -> None:
        if ctag is None:
            return  # Without a ctag there is nothing to invalidate against.
        if ctag != self._ctag:
            self._ctag = ctag
            self._windows.clear()
        self._windows[window] = documents
        while len(self._windows) > self._max_windows:
            # Bounded so paging through months forever cannot grow it without end.
            self._windows.pop(next(iter(self._windows)))


async def _read_limited(response: httpx.Response, limit: int) -> bytes | None:
    """Buffer a response body, or return None once it runs past ``limit``."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


async def fetch_calendar_documents(
    client: httpx.AsyncClient,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> list[str] | None:
    """Documents with VEVENTs overlapping ``start``..``end``.

    Returns None if the fetch failed, which an empty list would not distinguish
    from a genuinely empty calendar - and caching a failure would keep the
    calendar looking empty until the collection next changed.
    """
    url = settings.radicale_url.rstrip("/") + settings.shared_path
    query = CALENDAR_QUERY.format(start=format_caldav_time(start), end=format_caldav_time(end))
    # Streamed so an outsized collection is abandoned rather than buffered: any
    # authenticated user can PUT arbitrarily many events, and peak memory is a
    # multiple of the response once it is parsed into a DOM.
    try:
        async with client.stream(
            "REPORT", url, headers=_admin_headers(settings, Depth="1"), content=query
        ) as response:
            if response.status_code != 207:
                await response.aread()
                log.error(
                    "calendar-query REPORT failed: %s %s", response.status_code, response.text[:200]
                )
                return None
            content = await _read_limited(response, settings.max_calendar_bytes)
    except httpx.HTTPError as exc:
        log.error("calendar-query REPORT could not be sent: %s", exc)
        return None

    if content is None:
        log.error(
            "calendar-query response exceeded %s bytes; showing an empty calendar",
            settings.max_calendar_bytes,
        )
        return None
    return parse_calendar_data(content)


async def fetch_calendar_documents_cached(
    client: httpx.AsyncClient,
    settings: Settings,
    cache: CalendarCache,
    start: datetime,
    end: datetime,
) -> list[str]:
    """Fetch a window, reusing the cache while the collection is unchanged."""
    window = (format_caldav_time(start), format_caldav_time(end))
    ctag = await fetch_ctag(client, settings)

    cached = cache.get(ctag, window)
    if cached is not None:
        return cached

    documents = await fetch_calendar_documents(client, settings, start, end)
    if documents is None:
        return []
    cache.put(ctag, window, documents)
    return documents
