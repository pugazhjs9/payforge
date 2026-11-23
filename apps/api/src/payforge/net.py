"""Outgoing requests to URLs that merchants choose: guarding against SSRF.

A webhook URL is chosen by the merchant, and our server will POST to it. Without
checks, "http://169.254.169.254/…" (the cloud metadata service, which hands out
credentials) or "http://10.0.0.5:3306" would make PayForge attack its own network.

Rules: https only; no user:password@ in the URL; and EVERY address the host name
resolves to must be a public one. Checked when the endpoint is saved AND again before
each delivery, because DNS can change in between ("DNS rebinding"). Redirects are not
followed for the same reason. In production an egress proxy that enforces the same
rules at connect time closes the last gap (the resolve-then-connect race).
"""

import asyncio
import ipaddress
import socket

import httpx

from payforge.config import settings


class UnsafeUrl(ValueError):
    pass


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    # is_global is False for private, loopback, link-local (169.254/16), shared
    # (100.64/10), reserved, multicast and unspecified ranges
    return ip.is_global and not ip.is_multicast


async def check_webhook_url(url: str) -> None:
    """Raises UnsafeUrl if PayForge must not send requests to `url`."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise UnsafeUrl("not a valid URL") from exc
    insecure_ok = settings.allow_insecure_webhooks
    if parsed.scheme != "https" and not (insecure_ok and parsed.scheme == "http"):
        raise UnsafeUrl("must use https://")
    if parsed.userinfo:
        raise UnsafeUrl("must not contain a user name or password")
    if not parsed.host:
        raise UnsafeUrl("must have a host name")
    if insecure_ok:
        return  # local development: localhost and private addresses allowed

    try:
        addresses = await _resolve(parsed.host)
    except OSError as exc:
        raise UnsafeUrl("host name does not resolve") from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UnsafeUrl("must point to a public internet address")
