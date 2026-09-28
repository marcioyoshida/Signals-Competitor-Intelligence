"""Client ID Metadata Documents (MCP 2026-07-28, D3) — fetched SSRF-hardened (T8).

A client's ``client_id`` is an HTTPS URL we must fetch, so an attacker chooses where the
authorization server connects. Every rule below exists for that reason:

* HTTPS only, a path component, no userinfo, no fragment, default port, a hostname
  (never an IP literal).
* DNS is resolved ONCE; every address must be globally routable (no private, loopback,
  link-local incl. 169.254.169.254, CGNAT, ULA, multicast, reserved). We then connect
  to the address we checked — with TLS verified against the hostname — so a DNS
  rebind between "check" and "connect" can't redirect us.
* No redirects. At most MAX_BYTES read, TOTAL_TIMEOUT_S overall.
* The document must be a JSON object whose ``client_id`` equals the URL EXACTLY and
  whose ``redirect_uris`` are HTTPS or loopback HTTP.

The network is injected (``resolve``, ``fetch``) so each rejection is unit-tested.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
from typing import Any, Callable
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024
TOTAL_TIMEOUT_S = 3.0
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


class CIMDError(Exception):
    """A client_id URL or document we won't accept. ``str(e)`` is safe to show/log."""


def is_cimd_client_id(client_id: str) -> bool:
    return client_id.startswith("https://")


def validate_url(url: str) -> tuple[str, str]:
    """``(host, path_with_query)`` for an acceptable client_id URL, else CIMDError."""
    p = urlsplit(url)
    if p.scheme != "https":
        raise CIMDError("client_id must be an https URL")
    if p.username or p.password or "@" in p.netloc:
        raise CIMDError("client_id must not contain credentials")
    if p.fragment:
        raise CIMDError("client_id must not contain a fragment")
    if not p.path or p.path == "/":
        raise CIMDError("client_id must contain a path")
    if p.port not in (None, 443):
        raise CIMDError("client_id must use the default https port")
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        raise CIMDError("client_id must use a public hostname")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise CIMDError("client_id must use a hostname, not an IP address")
    except ValueError:
        pass
    return host, p.path + (("?" + p.query) if p.query else "")


def check_addresses(addrs: list[str]) -> str:
    """The first address, if EVERY resolved address is globally routable (T8)."""
    if not addrs:
        raise CIMDError("client_id host does not resolve")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise CIMDError("client_id host resolves to a non-public address")
    return addrs[0]


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def _fetch(ip: str, host: str, path: str, deadline: float) -> tuple[int, bytes]:
    """GET https://host/path, connecting to ``ip``, TLS verified for ``host``.
    No redirects, bounded body, bounded time."""
    import urllib3

    remaining = max(0.1, deadline - time.monotonic())
    pool = urllib3.HTTPSConnectionPool(
        ip, port=443, server_hostname=host, assert_hostname=host,
        cert_reqs="CERT_REQUIRED", retries=False,
        timeout=urllib3.Timeout(connect=min(2.0, remaining), read=min(2.0, remaining)))
    resp = pool.urlopen("GET", path, headers={"Host": host, "Accept": "application/json",
                                              "User-Agent": "onssa.org-oauth/1"},
                        redirect=False, preload_content=False)
    try:
        body = resp.read(MAX_BYTES + 1)
    finally:
        resp.release_conn()
    if time.monotonic() > deadline:
        raise CIMDError("client_id document fetch timed out")
    return resp.status, body


def _is_loopback_http(uri: str) -> bool:
    p = urlsplit(uri)
    return p.scheme == "http" and (p.hostname or "") in ("localhost", "127.0.0.1", "::1")


def validate_document(url: str, body: bytes) -> dict[str, Any]:
    if len(body) > MAX_BYTES:
        raise CIMDError("client_id document too large")
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CIMDError("client_id document is not JSON") from None
    if not isinstance(doc, dict):
        raise CIMDError("client_id document must be a JSON object")
    if doc.get("client_id") != url:
        raise CIMDError("client_id in the document does not match its URL")
    name = doc.get("client_name")
    if not isinstance(name, str) or not name.strip():
        raise CIMDError("client_id document needs a client_name")
    uris = doc.get("redirect_uris")
    if (not isinstance(uris, list) or not uris
            or not all(isinstance(u, str) for u in uris)):
        raise CIMDError("client_id document needs redirect_uris")
    for u in uris:
        p = urlsplit(u)
        if p.fragment or not (p.scheme == "https" or _is_loopback_http(u)):
            raise CIMDError("redirect_uris must be https or loopback http")
    return {"client_id": url, "client_name": name.strip()[:80], "redirect_uris": uris,
            "doc_hash": hashlib.sha256(body).hexdigest()[:32], "public": True}


def fetch(url: str, *, resolve: Callable[[str], list[str]] | None = None,
          fetcher: Callable[[str, str, str, float], tuple[int, bytes]] | None = None
          ) -> dict[str, Any]:
    """Fetch and validate one client_id metadata document (T8). Raises CIMDError."""
    host, path = validate_url(url)
    deadline = time.monotonic() + TOTAL_TIMEOUT_S
    try:
        ip = check_addresses((resolve or _resolve)(host))
    except CIMDError:
        raise
    except Exception:
        raise CIMDError("client_id host does not resolve") from None
    try:
        status, body = (fetcher or _fetch)(ip, host, path, deadline)
    except CIMDError:
        raise
    except Exception:
        raise CIMDError("client_id document could not be fetched") from None
    if status != 200:
        raise CIMDError("client_id document fetch returned %d" % status)
    return validate_document(url, body)


def redirect_allowed(requested: str, registered: list[str]) -> bool:
    """Exact match (T2) — except the PORT of a loopback redirect, which OAuth 2.1
    requires the server to accept as any port (desktop clients bind a random port)."""
    if requested in registered:
        return True
    rq = urlsplit(requested)
    if not _is_loopback_http(requested) or rq.fragment:
        return False
    for r in registered:
        rp = urlsplit(r)
        if (_is_loopback_http(r) and rp.hostname == rq.hostname and rp.path == rq.path
                and rp.query == rq.query and rp.username is None and rq.username is None):
            return True
    return False


def loopback_only(uris: list[str]) -> bool:
    return bool(uris) and all(_is_loopback_http(u) for u in uris)
