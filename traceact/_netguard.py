# traceact/_netguard.py
#
# Shared outbound-network safety guard. Used by every place TraceAct makes
# an outbound HTTP(S) call on the caller's behalf: HttpSink, OtlpSink, and
# the viewer's focus hook. One policy, one implementation, so a change here
# fixes all three instead of drifting apart between three copies.
#
# Threat model: TraceAct runs inside applications and agents that may pass
# attacker-influenced or misconfigured values into a URL a sink or hook then
# posts to. This guard stops that destination from silently reaching the
# private network (loopback aside — see below), a cloud metadata endpoint,
# or a redirect target the caller never validated, without the caller
# explicitly asking for it.
#
# Loopback (127.0.0.0/8, ::1) is always permitted regardless of policy: it's
# the machine TraceAct itself is running on, and the most common legitimate
# destination (a local collector, the traceact-browser relay). Everything
# else classed as private/link-local/reserved/multicast/unspecified is
# blocked unless the caller passes allow_private_network=True.

import ipaddress
import socket
import urllib.request
from typing import FrozenSet, Optional, Union
from urllib.parse import urlsplit

_IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class NetworkGuardError(ValueError):
    """A destination failed the outbound network safety check."""


class NetworkGuardWarning(UserWarning):
    """Raised via warnings.warn() when network_policy="warn" flags a destination."""


def _unwrap_ipv4_mapped(ip: _IpAddress) -> _IpAddress:
    """
    Return the IPv4 address inside an IPv4-mapped IPv6 literal (::ffff:a.b.c.d),
    or ``ip`` unchanged for anything else.

    Without this, a resolver or attacker-influenced DNS answer expressed as
    ``::ffff:169.254.169.254`` would classify as an ordinary public-looking
    IPv6 address and slip past the private/metadata check that the plain
    IPv4 form would have caught.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_loopback(ip_str: str) -> bool:
    """True if ip_str (an IP literal) is a loopback address."""
    ip = _unwrap_ipv4_mapped(ipaddress.ip_address(ip_str))
    return ip.is_loopback


def is_blocked_nonloopback(ip_str: str) -> bool:
    """
    True if ip_str is private, link-local, reserved, multicast, or
    unspecified — everything outside the public internet except loopback,
    which is handled separately (see module docstring).
    """
    ip = _unwrap_ipv4_mapped(ipaddress.ip_address(ip_str))
    if ip.is_loopback:
        return False
    return ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def resolve_host(host: str, port: int) -> FrozenSet[str]:
    """
    Resolve host to the set of IP literals it answers to.

    A thin wrapper around socket.getaddrinfo so tests can mock this one
    call (traceact._netguard.socket.getaddrinfo) instead of touching live
    DNS or the network.
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return frozenset(str(info[4][0]) for info in infos)


def check_destination(url: str, *, allow_private_network: bool = False,
                      allow_insecure_http: Optional[bool] = None) -> None:
    """
    Validate url as an outbound destination. Raises NetworkGuardError on
    any violation; returns None when the destination is acceptable.

    allow_private_network:
        When False (default), a hostname that resolves to any private,
        link-local, reserved, multicast, or unspecified address is
        rejected. A hostname with even one such answer among several is
        rejected outright — a caller who controls DNS could otherwise
        alternate between a public answer (seen here) and a private one
        (used by whichever address ends up connecting). Loopback is never affected
        by this flag; it's always permitted.

    allow_insecure_http:
        Governs plain http:// specifically. None (default) permits it only
        to a loopback destination — the common "local collector" case.
        True permits it to any destination allow_private_network already
        permits. False refuses plain http:// outright, even to loopback,
        requiring https:// everywhere.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise NetworkGuardError(f"unsupported scheme {parts.scheme!r} in {url!r} — use http or https")
    if "@" in (parts.netloc or ""):
        raise NetworkGuardError(f"URLs with embedded userinfo (user:pass@host) are not allowed: {url!r}")
    host = parts.hostname
    if not host:
        raise NetworkGuardError(f"no host in URL: {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    try:
        # An IP literal needs no DNS round trip — classify it directly.
        ipaddress.ip_address(host)
        addresses: FrozenSet[str] = frozenset([host])
    except ValueError:
        try:
            addresses = resolve_host(host, port)
        except OSError as exc:
            raise NetworkGuardError(f"could not resolve {host!r}: {exc}") from exc
    if not addresses:
        raise NetworkGuardError(f"no addresses resolved for {host!r}")

    if not allow_private_network:
        blocked = [a for a in addresses if is_blocked_nonloopback(a)]
        if blocked:
            raise NetworkGuardError(
                f"{host!r} resolves to a private/link-local/reserved address "
                f"({blocked[0]}) — pass allow_private_network=True to permit this"
            )

    if parts.scheme == "http":
        loopback_only = all(is_loopback(a) for a in addresses)
        if allow_insecure_http is False or (allow_insecure_http is None and not loopback_only):
            raise NetworkGuardError(
                f"plain http:// is only allowed to a loopback destination by default "
                f"({url!r} resolves to {sorted(addresses)}) — pass allow_insecure_http=True "
                f"to permit this, or use https://"
            )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """
    Refuses every redirect. A validated destination that answers with a
    redirect to somewhere else entirely bypasses whatever check just ran on
    the original URL — so nothing here follows one, ever.

    redirect_request() returning None doesn't hand the 3xx response back to
    the caller as a normal response — urllib falls through to
    HTTPDefaultErrorHandler, which raises HTTPError with the original
    status code (confirmed via tests/test_netguard.py, not assumed). Every
    call site here already catches that broadly (HttpSink/OtlpSink's
    `except Exception`, the focus hook's `except HTTPError as exc: status
    = exc.code`), so a refused redirect surfaces the same way any other
    delivery failure does.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Built once, reused by every guarded call — never installed as the process
# default (urllib.request.install_opener), which would silently change
# redirect behavior for unrelated code sharing this process. Every TraceAct
# call site opts in explicitly by using this opener instead of the bare
# urlopen() free function.
_OPENER = urllib.request.build_opener(_NoRedirect())


def open_guarded(req: urllib.request.Request, timeout: float):
    """
    Send req through the shared no-redirect opener. Same call shape as
    urllib.request.urlopen(req, timeout=timeout), minus redirect-following.
    """
    return _OPENER.open(req, timeout=timeout)
