"""Synthetic DNS, routes, sockets and TLS; never touch the real VPN/network."""
import socket
import ssl
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request

from app.vpn_transport import (GOOGLE_HOST, GOOGLE_PORT, IP_BOUND_IF,
    IPV6_BOUND_IF, NoRedirect, VPNError, VPNHTTPSConnection, VPNHTTPSHandler,
    make_vpn_opener, vpn_problem)


V4 = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.10", GOOGLE_PORT))
V6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:db8::10", GOOGLE_PORT, 0, 0))


class FakeSocket:
    def __init__(self, events, *, connect_error=None, bind_error=None):
        self.events = events
        self.connect_error = connect_error
        self.bind_error = bind_error
        self.closed = False
    def settimeout(self, timeout):
        self.events.append(("timeout", timeout))
    def setsockopt(self, level, option, value):
        self.events.append(("bind", level, option, value))
        if self.bind_error:
            raise self.bind_error
    def connect(self, address):
        self.events.append(("connect", address))
        if self.connect_error:
            raise self.connect_error
    def close(self):
        self.closed = True
        self.events.append(("close",))


class FakeTLS:
    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True
    def __init__(self, events, *, error=None):
        self.events = events
        self.error = error
    def wrap_socket(self, plain, *, server_hostname):
        self.events.append(("tls", server_hostname))
        if self.error:
            raise self.error
        return plain


class VPNTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.plain = FakeSocket(self.events)
        self.dns = Mock(return_value=[V4])
        self.socket_factory = Mock(return_value=self.plain)
        self.index = Mock(return_value=9)
        # Replace the module's dependency references only. Global socket/DNS
        # functions remain untouched, including in the standard TLS library.
        fake_socket = SimpleNamespace(**{name: getattr(socket, name) for name in
            ("AF_INET", "AF_INET6", "SOCK_STREAM", "IPPROTO_TCP", "IPPROTO_IP",
             "IPPROTO_IPV6", "_GLOBAL_DEFAULT_TIMEOUT")}, getaddrinfo=self.dns,
            if_nametoindex=self.index, socket=self.socket_factory)
        self.route = Mock(side_effect=self.normal_route)
        dependencies = (patch("app.vpn_transport.socket", fake_socket),
            patch("app.vpn_transport.sys", SimpleNamespace(platform="darwin")),
            patch("app.vpn_transport.subprocess", SimpleNamespace(run=self.route,
                SubprocessError=subprocess.SubprocessError)))
        for dependency in dependencies:
            dependency.start()
            self.addCleanup(dependency.stop)

    def normal_route(self, args, **kwargs):
        self.events.append(("route", tuple(args)))
        return SimpleNamespace(returncode=0,
            stdout="route to: synthetic\n destination: synthetic\n interface: utun8\n flags: synthetic\n")

    def connect(self, **kwargs):
        connection = VPNHTTPSConnection(GOOGLE_HOST, timeout=10,
            context=FakeTLS(self.events, **kwargs))
        connection.connect()
        return connection

    def test_preflight_checks_vpn_without_opening_socket(self):
        self.assertIsNone(vpn_problem())
        self.socket_factory.assert_not_called()
        self.dns.assert_called_once_with(GOOGLE_HOST, GOOGLE_PORT,
            type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        self.index.assert_called_once_with("utun8")
        self.route.assert_called_once_with(["/sbin/route", "-n", "get", "192.0.2.10"],
            capture_output=True, text=True, check=False, timeout=3)

    def test_no_vpn_normal_interface_fails_before_socket(self):
        self.route.return_value = SimpleNamespace(returncode=0, stdout="interface: en0\n")
        self.route.side_effect = None
        self.assertEqual(vpn_problem(), "vpn_required")
        with self.assertRaisesRegex(VPNError, "vpn_required"):
            self.connect()
        self.socket_factory.assert_not_called()
        self.index.assert_not_called()

    def test_non_macos_fails_before_dns_or_socket(self):
        with patch("app.vpn_transport.sys", SimpleNamespace(platform="linux")):
            self.assertEqual(vpn_problem(), "vpn_required")
            with self.assertRaisesRegex(VPNError, "vpn_required"):
                self.connect()
        self.dns.assert_not_called()
        self.socket_factory.assert_not_called()

    def test_dns_or_route_error_is_stable_and_no_socket(self):
        self.dns.side_effect = OSError("private system diagnostic")
        self.assertEqual(vpn_problem(), "vpn_route_unavailable")
        self.dns.side_effect = None
        self.route.side_effect = subprocess.TimeoutExpired("synthetic", 3)
        self.assertEqual(vpn_problem(), "vpn_route_unavailable")
        self.route.side_effect = None
        for result in (SimpleNamespace(returncode=1, stdout="private system diagnostic"),
                SimpleNamespace(returncode=0, stdout="no interface")):
            self.route.return_value = result
            self.assertEqual(vpn_problem(), "vpn_route_unavailable")
        self.socket_factory.assert_not_called()

    def test_v4_binds_dynamic_vpn_before_single_tcp_and_normal_tls(self):
        connection = self.connect()
        self.socket_factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        bind = ("bind", socket.IPPROTO_IP, IP_BOUND_IF, 9)
        self.assertIn(bind, self.events)
        self.assertLess(self.events.index(bind), self.events.index(("connect", V4[4])))
        self.assertEqual([event for event in self.events if event[0] == "connect"], [("connect", V4[4])])
        self.assertEqual(self.events[-1], ("tls", GOOGLE_HOST))
        self.assertIs(connection.sock, self.plain)
        self.assertEqual(self.route.call_count, 3)

    def test_v6_binds_ipv6_interface_and_ipv6_route_command(self):
        self.dns.return_value = [V6]
        self.connect()
        self.socket_factory.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        self.assertIn(("bind", socket.IPPROTO_IPV6, IPV6_BOUND_IF, 9), self.events)
        self.assertIn(("connect", V6[4]), self.events)
        for invocation in self.route.call_args_list:
            self.assertEqual(invocation.args[0], ["/sbin/route", "-n", "get", "-inet6", "2001:db8::10"])

    def test_first_available_vpn_routed_address_selected(self):
        self.dns.return_value = [V6, V4]
        def routes(args, **kwargs):
            interface = "en0" if args[-1] == V6[4][0] else "ppp4"
            return SimpleNamespace(returncode=0, stdout="interface: " + interface + "\n")
        self.route.side_effect = routes
        self.connect()
        self.assertIn(("connect", V4[4]), self.events)
        self.assertEqual(self.socket_factory.call_count, 1)
        self.assertTrue(all(call.args == ("ppp4",) for call in self.index.call_args_list))

    def test_tcp_failure_never_falls_back_to_second_dns_address(self):
        self.dns.return_value = [V4, V6]
        self.plain.connect_error = TimeoutError("synthetic connection timeout")
        with self.assertRaises(TimeoutError):
            self.connect()
        self.assertEqual(self.socket_factory.call_count, 1)
        self.assertEqual([event for event in self.events if event[0] == "connect"], [("connect", V4[4])])
        self.assertTrue(self.plain.closed)
        self.assertFalse(any(event[0] == "tls" for event in self.events))

    def test_vpn_drop_before_connect_rejected_and_socket_closed(self):
        self.route.side_effect = [SimpleNamespace(returncode=0, stdout="interface: utun8\n"),
            SimpleNamespace(returncode=0, stdout="interface: en0\n")]
        with self.assertRaisesRegex(VPNError, "vpn_required"):
            self.connect()
        self.assertTrue(self.plain.closed)
        self.assertFalse(any(event[0] in {"connect", "tls"} for event in self.events))

    def test_route_to_other_vpn_interface_rejected_before_connect(self):
        self.route.side_effect = [SimpleNamespace(returncode=0, stdout="interface: utun8\n"),
            SimpleNamespace(returncode=0, stdout="interface: utun9\n")]
        with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
            self.connect()
        self.assertTrue(self.plain.closed)
        self.assertFalse(any(event[0] == "connect" for event in self.events))

    def test_vpn_drop_after_tcp_rejected_before_tls_or_http(self):
        self.route.side_effect = [SimpleNamespace(returncode=0, stdout="interface: utun8\n"),
            SimpleNamespace(returncode=0, stdout="interface: utun8\n"),
            SimpleNamespace(returncode=0, stdout="interface: en0\n")]
        with self.assertRaisesRegex(VPNError, "vpn_required"):
            self.connect()
        self.assertTrue(self.plain.closed)
        self.assertEqual(len([event for event in self.events if event[0] == "connect"]), 1)
        self.assertFalse(any(event[0] == "tls" for event in self.events))

    def test_interface_index_changes_fail_before_connect(self):
        self.index.side_effect = [9, 10]
        with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
            self.connect()
        self.assertTrue(self.plain.closed)
        self.assertFalse(any(event[0] == "connect" for event in self.events))

    def test_interface_binding_failure_never_connects_or_falls_back(self):
        self.plain.bind_error = OSError("synthetic binding failure")
        with self.assertRaises(OSError):
            self.connect()
        self.assertTrue(self.plain.closed)
        self.assertFalse(any(event[0] == "connect" for event in self.events))
        self.assertEqual(self.socket_factory.call_count, 1)

    def test_tls_verification_failure_closes_socket_without_retry(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.connect(error=ssl.SSLCertVerificationError("synthetic certificate failure"))
        self.assertTrue(self.plain.closed)
        self.assertEqual(self.socket_factory.call_count, 1)

    def test_fixed_native_host_port_and_no_tunnel(self):
        for host, port in (("other.example", None), (GOOGLE_HOST, 80), ("192.0.2.10", 443)):
            with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
                VPNHTTPSConnection(host, port, context=FakeTLS(self.events))
        connection = VPNHTTPSConnection(GOOGLE_HOST, context=FakeTLS(self.events))
        with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
            connection.set_tunnel("other.example")
        self.socket_factory.assert_not_called()

    def test_insecure_tls_context_refused(self):
        context = FakeTLS(self.events)
        context.check_hostname = False
        with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
            VPNHTTPSConnection(GOOGLE_HOST, context=context)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_NONE
        with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
            VPNHTTPSConnection(GOOGLE_HOST, context=context)
        self.socket_factory.assert_not_called()

    def test_https_handler_rejects_proxy_alternate_host_and_port_before_dns(self):
        handler = VPNHTTPSHandler()
        handler.do_open = Mock()
        for url in ("https://other.example/", "https://" + GOOGLE_HOST + ":80/",
                "https://user:password@" + GOOGLE_HOST + "/"):
            with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
                handler.https_open(Request(url))
        proxy = Request("https://" + GOOGLE_HOST + "/")
        proxy.set_proxy("proxy.example:3128", "http")
        with self.assertRaisesRegex(VPNError, "vpn_route_unavailable"):
            handler.https_open(proxy)
        handler.do_open.assert_not_called()
        self.dns.assert_not_called()

    def test_https_handler_uses_bound_connection_with_verified_context(self):
        handler = VPNHTTPSHandler()
        handler.do_open = Mock(return_value="synthetic response")
        request = Request("https://" + GOOGLE_HOST + "/v1beta/models/gemini-3.8-flash:generateContent")
        self.assertEqual(handler.https_open(request), "synthetic response")
        handler.do_open.assert_called_once_with(VPNHTTPSConnection, request, context=handler._context)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(handler._context.check_hostname, True)

    def test_opener_has_no_http_ftp_file_fallback_or_redirect(self):
        opener = make_vpn_opener()
        for url in ("http://" + GOOGLE_HOST + "/", "ftp://other.example/", "file:///synthetic"):
            with self.assertRaises(URLError):
                opener.open(url)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/"))
        self.socket_factory.assert_not_called()
        self.dns.assert_not_called()


if __name__ == "__main__":
    unittest.main()
