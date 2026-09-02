# tests/test_netguard.py
#
# Adversarial tests for traceact._netguard: the shared outbound-network
# guard used by HttpSink, OtlpSink, and the viewer's focus hook.
#
# IP-literal classification needs no DNS and is tested directly. Hostname
# classification mocks socket.getaddrinfo — live DNS in a test suite is
# the flakiness CODING.md's testing rules forbid. Redirect refusal uses a
# live local loopback server (fast, offline, no external network).

import http.server
import socket
import threading
import unittest.mock as mock

import pytest

from traceact import _netguard
from traceact._netguard import (
    NetworkGuardError,
    check_destination,
    is_blocked_nonloopback,
    is_loopback,
    open_guarded,
    resolve_host,
)


# ---------------------------------------------------------------------------
# IP-literal classification — no DNS, no mocking needed
# ---------------------------------------------------------------------------

class TestIsLoopback:
    def test_ipv4_loopback(self):
        assert is_loopback("127.0.0.1") is True

    def test_ipv4_loopback_other_address_in_range(self):
        assert is_loopback("127.5.5.5") is True

    def test_ipv6_loopback(self):
        assert is_loopback("::1") is True

    def test_public_ipv4_is_not_loopback(self):
        assert is_loopback("93.184.216.34") is False

    def test_private_ipv4_is_not_loopback(self):
        assert is_loopback("10.0.0.1") is False

    def test_ipv4_mapped_ipv6_loopback(self):
        # A resolver or attacker-influenced answer can express loopback as
        # an IPv4-mapped IPv6 literal; it must classify the same as the
        # plain IPv4 form, not slip through as "just some IPv6 address".
        assert is_loopback("::ffff:127.0.0.1") is True


class TestIsBlockedNonloopback:
    def test_cloud_metadata_address_is_blocked(self):
        assert is_blocked_nonloopback("169.254.169.254") is True

    def test_rfc1918_10_range_is_blocked(self):
        assert is_blocked_nonloopback("10.0.0.5") is True

    def test_rfc1918_172_range_is_blocked(self):
        assert is_blocked_nonloopback("172.16.0.5") is True

    def test_rfc1918_192_range_is_blocked(self):
        assert is_blocked_nonloopback("192.168.1.5") is True

    def test_link_local_ipv4_is_blocked(self):
        assert is_blocked_nonloopback("169.254.1.1") is True

    def test_ipv6_link_local_is_blocked(self):
        assert is_blocked_nonloopback("fe80::1") is True

    def test_ipv6_unique_local_is_blocked(self):
        assert is_blocked_nonloopback("fc00::1") is True

    def test_unspecified_ipv4_is_blocked(self):
        assert is_blocked_nonloopback("0.0.0.0") is True

    def test_multicast_is_blocked(self):
        assert is_blocked_nonloopback("224.0.0.1") is True

    def test_loopback_is_not_in_this_bucket(self):
        # Loopback is handled separately (always allowed) — it must not
        # also count as a "blocked" private address, or every default
        # loopback deployment (traceact-browser's own relay, an OTLP
        # collector on localhost) would be rejected outright.
        assert is_blocked_nonloopback("127.0.0.1") is False

    def test_public_ipv4_is_not_blocked(self):
        assert is_blocked_nonloopback("93.184.216.34") is False

    def test_ipv4_mapped_ipv6_metadata_address_is_blocked(self):
        assert is_blocked_nonloopback("::ffff:169.254.169.254") is True


# ---------------------------------------------------------------------------
# check_destination — structural checks (no DNS)
# ---------------------------------------------------------------------------

class TestStructuralChecks:
    def test_rejects_unsupported_scheme(self):
        with pytest.raises(NetworkGuardError, match="scheme"):
            check_destination("ftp://example.com/traces")

    def test_rejects_userinfo(self):
        with pytest.raises(NetworkGuardError, match="userinfo"):
            check_destination("https://user:pass@example.com/traces")

    def test_rejects_missing_host(self):
        with pytest.raises(NetworkGuardError, match="host"):
            check_destination("https:///traces")


# ---------------------------------------------------------------------------
# check_destination — IP-literal destinations (no DNS)
# ---------------------------------------------------------------------------

class TestIpLiteralDestinations:
    def test_public_https_ip_literal_allowed_by_default(self):
        check_destination("https://93.184.216.34/traces")  # must not raise

    def test_private_ip_literal_blocked_by_default(self):
        with pytest.raises(NetworkGuardError, match="private"):
            check_destination("https://10.0.0.5/traces")

    def test_metadata_ip_literal_blocked_by_default(self):
        with pytest.raises(NetworkGuardError, match="private"):
            check_destination("http://169.254.169.254/latest/meta-data/")

    def test_private_ip_literal_allowed_with_flag(self):
        check_destination("https://10.0.0.5/traces", allow_private_network=True)

    def test_loopback_http_allowed_by_default(self):
        check_destination("http://127.0.0.1:4318/traces")  # must not raise

    def test_public_http_ip_literal_blocked_by_default(self):
        # Plain HTTP to a non-loopback address is refused by default, even
        # when the address itself is public (not private/blocked) — the
        # insecure-transport check is independent of the private-network one.
        with pytest.raises(NetworkGuardError, match="http"):
            check_destination("http://93.184.216.34/traces")

    def test_public_http_allowed_with_allow_insecure_http_true(self):
        check_destination("http://93.184.216.34/traces", allow_insecure_http=True)

    def test_loopback_http_blocked_with_allow_insecure_http_false(self):
        with pytest.raises(NetworkGuardError, match="http"):
            check_destination("http://127.0.0.1:4318/traces", allow_insecure_http=False)

    def test_public_https_ip_literal_never_needs_insecure_http_flag(self):
        # allow_insecure_http only gates plain http:// — https is unaffected.
        check_destination("https://93.184.216.34/traces", allow_insecure_http=False)


# ---------------------------------------------------------------------------
# check_destination — hostname destinations (mocked DNS, no live network)
# ---------------------------------------------------------------------------

def _fake_getaddrinfo(ip: str):
    """A socket.getaddrinfo stand-in resolving any host to one address."""
    def fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return fake


def _fake_getaddrinfo_multi(*ips: str):
    """Resolves to several addresses, for the mixed-answer rebinding case."""
    def fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return fake


class TestHostnameDestinations:
    def test_hostname_resolving_public_allowed(self):
        with mock.patch.object(_netguard.socket, "getaddrinfo",
                               _fake_getaddrinfo("93.184.216.34")):
            check_destination("https://collector.example.com/traces")

    def test_hostname_resolving_private_blocked(self):
        with mock.patch.object(_netguard.socket, "getaddrinfo",
                               _fake_getaddrinfo("10.0.0.5")):
            with pytest.raises(NetworkGuardError, match="private"):
                check_destination("https://internal.example.com/traces")

    def test_hostname_resolving_loopback_allowed_over_http(self):
        with mock.patch.object(_netguard.socket, "getaddrinfo",
                               _fake_getaddrinfo("127.0.0.1")):
            check_destination("http://localhost:4318/traces")

    def test_hostname_with_one_public_one_private_answer_rejected(self):
        # DNS rebinding / multi-answer case: even one private answer among
        # several rejects the whole hostname, since either address could
        # be the one that ends up connecting.
        with mock.patch.object(
            _netguard.socket, "getaddrinfo",
            _fake_getaddrinfo_multi("93.184.216.34", "10.0.0.5"),
        ):
            with pytest.raises(NetworkGuardError, match="private"):
                check_destination("https://mixed.example.com/traces")

    def test_dns_resolution_failure_raises_guard_error(self):
        def raise_gaierror(host, port, **kwargs):
            raise socket.gaierror("nodename nor servname provided")

        with mock.patch.object(_netguard.socket, "getaddrinfo", raise_gaierror):
            with pytest.raises(NetworkGuardError, match="could not resolve"):
                check_destination("https://nowhere.invalid/traces")

    def test_resolve_host_returns_frozenset_of_addresses(self):
        with mock.patch.object(_netguard.socket, "getaddrinfo",
                               _fake_getaddrinfo("93.184.216.34")):
            assert resolve_host("collector.example.com", 443) == frozenset(["93.184.216.34"])


# ---------------------------------------------------------------------------
# Redirect refusal — a live local loopback server, no external network
# ---------------------------------------------------------------------------

def _start_redirecting_server(target_url: str):
    """A local server that answers every request with a 302 to target_url."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}/", shutdown


def _start_capturing_server():
    """A local server recording whether it was ever hit."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}/elsewhere", hits, shutdown


class TestPublicExport:
    def test_guard_types_importable_from_top_level(self):
        # USAGE.md names NetworkGuardWarning as the thing warn mode emits —
        # it (and the error it reports on) must be reachable without
        # importing the private module.
        from traceact import NetworkGuardError as PublicError
        from traceact import NetworkGuardWarning as PublicWarning
        assert PublicError is NetworkGuardError
        assert PublicWarning is _netguard.NetworkGuardWarning


class TestRedirectRefusal:
    def test_redirect_is_not_followed(self):
        import urllib.error
        import urllib.request

        target_url, target_hits, target_shutdown = _start_capturing_server()
        redirect_url, redirect_shutdown = _start_redirecting_server(target_url)
        try:
            req = urllib.request.Request(redirect_url, data=b"{}", method="POST")
            # A refused redirect surfaces as HTTPError carrying the original
            # 302, not a followed request — see _NoRedirect's docstring.
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                open_guarded(req, timeout=2.0)
            assert exc_info.value.code == 302
            assert target_hits == []
        finally:
            redirect_shutdown()
            target_shutdown()

    def test_ordinary_request_still_succeeds(self):
        import urllib.request

        target_url, target_hits, target_shutdown = _start_capturing_server()
        try:
            req = urllib.request.Request(target_url, data=b"{}", method="POST")
            resp = open_guarded(req, timeout=2.0)
            assert resp.status == 200
            assert len(target_hits) == 1
        finally:
            target_shutdown()
