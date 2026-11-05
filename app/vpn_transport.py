"""Native Google HTTPS bound to an existing macOS VPN interface.

This module only observes DNS and routes. It never starts, stops, or configures
a VPN. A missing VPN route fails closed before opening a TCP socket; an existing
route is rechecked and the socket is bound to that interface before connecting.
"""
from dataclasses import dataclass
import http.client
import ipaddress
import re
import socket
import ssl
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import (HTTPDefaultErrorHandler, HTTPErrorProcessor,
    HTTPRedirectHandler, HTTPSHandler, OpenerDirector, ProxyHandler, UnknownHandler)

VERSION = "m17-vpn-transport-1.0.0"
GOOGLE_HOST = "generativelanguage.googleapis.com"
GOOGLE_PORT = 443
IP_BOUND_IF = 25
IPV6_BOUND_IF = 125
ROUTE_TIMEOUT_SECONDS = 3
VPN_INTERFACE = re.compile(r"(?:utun|tun|ppp)[0-9]+\Z")


class VPNError(OSError):
    """Stable local network codes; no command output or credentials."""


@dataclass(frozen=True)
class VPNAddress:
    family: int
    socktype: int
    protocol: int
    sockaddr: tuple
    interface: str
    interface_index: int


def _route_interface(address, family):
    try:
        ip = ipaddress.ip_address(address)
        if (family == socket.AF_INET and ip.version != 4
                or family == socket.AF_INET6 and ip.version != 6
                or family not in {socket.AF_INET, socket.AF_INET6}):
            raise VPNError("vpn_route_unavailable")
        args = ["/sbin/route", "-n", "get"]
        if family == socket.AF_INET6:
            args.append("-inet6")
        args.append(str(ip))
        result = subprocess.run(args, capture_output=True, text=True,
            check=False, timeout=ROUTE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise VPNError("vpn_route_unavailable")
        interfaces = re.findall(r"(?m)^\s*interface:\s*(\S+)\s*$", result.stdout)
        if len(interfaces) != 1:
            raise VPNError("vpn_route_unavailable")
        return interfaces[0]
    except VPNError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise VPNError("vpn_route_unavailable") from None


def _vpn_index(interface):
    if not isinstance(interface, str) or VPN_INTERFACE.fullmatch(interface) is None:
        raise VPNError("vpn_required")
    try:
        index = socket.if_nametoindex(interface)
    except (OSError, ValueError):
        raise VPNError("vpn_route_unavailable") from None
    if type(index) is not int or index <= 0:
        raise VPNError("vpn_route_unavailable")
    return index


def _select_vpn_address():
    if sys.platform != "darwin":
        raise VPNError("vpn_required")
    try:
        addresses = socket.getaddrinfo(GOOGLE_HOST, GOOGLE_PORT,
            type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        raise VPNError("vpn_route_unavailable") from None
    seen, found_normal_route = set(), False
    for family, socktype, protocol, _canonname, sockaddr in addresses:
        if family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP:
            continue
        if (not isinstance(sockaddr, tuple) or len(sockaddr) != (2 if family == socket.AF_INET else 4)
                or sockaddr[1] != GOOGLE_PORT or not isinstance(sockaddr[0], str)):
            continue
        identity = (family, sockaddr)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            interface = _route_interface(sockaddr[0], family)
            index = _vpn_index(interface)
        except VPNError as error:
            found_normal_route = found_normal_route or str(error) == "vpn_required"
            continue
        return VPNAddress(family, socktype, protocol, sockaddr, interface, index)
    raise VPNError("vpn_required" if found_normal_route else "vpn_route_unavailable")


def vpn_problem():
    """Return a stable admission reason without opening a TCP connection."""
    try:
        _select_vpn_address()
    except VPNError as error:
        return str(error)
    return None


def _assert_same_route(address):
    interface = _route_interface(address.sockaddr[0], address.family)
    index = _vpn_index(interface)
    if interface != address.interface or index != address.interface_index:
        raise VPNError("vpn_route_unavailable")


class VPNHTTPSConnection(http.client.HTTPSConnection):
    """One TCP attempt to Google, interface-bound, using verified standard TLS."""

    def __init__(self, host, port=None, **kwargs):
        super().__init__(host, port, **kwargs)
        if self.host != GOOGLE_HOST or self.port != GOOGLE_PORT:
            raise VPNError("vpn_route_unavailable")
        if self.source_address is not None:
            raise VPNError("vpn_route_unavailable")
        if self._context.verify_mode != ssl.CERT_REQUIRED or self._context.check_hostname is not True:
            raise VPNError("vpn_route_unavailable")

    def set_tunnel(self, host, port=None, headers=None):
        raise VPNError("vpn_route_unavailable")

    def connect(self):
        if self.host != GOOGLE_HOST or self.port != GOOGLE_PORT or self._tunnel_host:
            raise VPNError("vpn_route_unavailable")
        address = _select_vpn_address()
        plain = None
        try:
            plain = socket.socket(address.family, address.socktype, address.protocol)
            if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                plain.settimeout(self.timeout)
            level, option = ((socket.IPPROTO_IP, IP_BOUND_IF) if address.family == socket.AF_INET
                else (socket.IPPROTO_IPV6, IPV6_BOUND_IF))
            plain.setsockopt(level, option, address.interface_index)
            # Binding makes the route check atomic in effect: a dropped VPN
            # cannot make this socket use a normal interface after this check.
            _assert_same_route(address)
            plain.connect(address.sockaddr)
            _assert_same_route(address)
            self.sock = self._context.wrap_socket(plain, server_hostname=GOOGLE_HOST)
        except BaseException:
            if plain is not None:
                plain.close()
            self.sock = None
            raise


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VPNHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        try:
            url = urlsplit(req.full_url)
            allowed = (url.scheme == "https" and url.hostname == GOOGLE_HOST
                and url.port in {None, GOOGLE_PORT} and url.username is None and url.password is None
                and not req.has_proxy() and not req._tunnel_host)
        except ValueError:
            allowed = False
        if not allowed:
            raise VPNError("vpn_route_unavailable")
        return self.do_open(VPNHTTPSConnection, req, context=self._context)


def make_vpn_opener():
    """HTTPS-only opener; system proxies and redirects are never used."""
    opener = OpenerDirector()
    for handler in (ProxyHandler({}), UnknownHandler(), VPNHTTPSHandler(),
                    HTTPDefaultErrorHandler(), NoRedirect(), HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener
