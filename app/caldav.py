"""Server-side CalDAV client used by the web viewer and by startup bootstrap."""

from __future__ import annotations

import logging
from xml.etree import ElementTree

import httpx

from .config import Settings

log = logging.getLogger(__name__)

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

CALENDAR_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT"/>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""

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
<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>
"""


def _admin_headers(settings: Settings, **extra: str) -> dict[str, str]:
    headers = {"X-Remote-User": settings.shared_principal, "Content-Type": "application/xml; charset=utf-8"}
    headers.update(extra)
    return headers


async def ensure_collection(client: httpx.AsyncClient, settings: Settings) -> None:
    """Create the shared calendar collection on first run."""
    url = settings.radicale_url.rstrip("/") + settings.shared_path
    probe = await client.request(
        "PROPFIND", url, headers=_admin_headers(settings, Depth="0"), content=PROPFIND_EXISTS
    )
    if probe.status_code in (207, 200):
        log.info("shared calendar already present at %s", settings.shared_path)
        return
    if probe.status_code not in (403, 404, 409):
        log.warning("unexpected status %s probing shared calendar", probe.status_code)

    body = MKCALENDAR_BODY.format(display_name=settings.shared_display_name)
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


async def fetch_calendar_documents(client: httpx.AsyncClient, settings: Settings) -> list[str]:
    """Fetch every VEVENT-bearing document in the shared collection."""
    url = settings.radicale_url.rstrip("/") + settings.shared_path
    response = await client.request(
        "REPORT", url, headers=_admin_headers(settings, Depth="1"), content=CALENDAR_QUERY
    )
    if response.status_code != 207:
        log.error("calendar-query REPORT failed: %s %s", response.status_code, response.text[:200])
        return []
    return parse_calendar_data(response.content)
