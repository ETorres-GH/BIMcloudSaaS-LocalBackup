"""Which addresses may receive BIMcloud credentials (tokens, tickets, session ids)."""

from __future__ import annotations

from urllib.parse import urlparse

# Domains of BIMcloud SaaS, where `<escritorio>.<domain>` has its data server at
# `<escritorio>-data.<domain>`. A closed list on purpose.
SAAS_DOMAINS = ("bimcloud.com",)


def is_trusted_host(host: str, server_url: str) -> bool:
    """True only for the BIMcloud server itself or, on BIMcloud SaaS, its data server.

    A SaaS server is `<escritorio>.<one of SAAS_DOMAINS>` (exactly one label before the
    domain), and its data server is `<escritorio>-data.<domain>`: `escritorio.bimcloud.com`
    trusts `escritorio-data.bimcloud.com` and nothing else. No suffix is ever trusted, since
    other tenants share the domain. Any other server (a BIMcloud of the office's own, a
    .com.br domain...) trusts only its exact host; its data server must then match the exact
    connectionUrls.
    """
    server = _normalize_host(urlparse(server_url).hostname)
    host = _normalize_host(host)
    if not server or not host:
        return False
    if host == server:
        return True
    tenant, _, domain = server.partition(".")
    return domain in SAAS_DOMAINS and host == f"{tenant}-data.{domain}"


def _normalize_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def https_origin(url: str) -> tuple[str, int] | None:
    """(host, port) of an https URL, with the default port filled in; None otherwise."""
    parsed = urlparse(url)
    host = _normalize_host(parsed.hostname)
    if parsed.scheme.lower() != "https" or not host:
        return None
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return host, port
