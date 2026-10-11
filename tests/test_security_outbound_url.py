"""
Tests for security.validate_outbound_url — the SSRF defence used by the
ntfy/notification settings and the custom RSS feed
endpoint (already prod-tested).

Covers:
- Scheme enforcement (HTTPS only by default)
- Hostname normalisation + allowlist
- Bare IP literal rejection
- RFC1918 / loopback / link-local / cloud metadata rejection via
  resolved-IP inspection (with mocked socket.getaddrinfo)
- DNS rebind defence: only the FIRST resolution is consulted, the
  pinned IP is returned for the caller to dial.
"""

import socket
from unittest.mock import patch

import pytest


from security import validate_outbound_url


def _v4(*octets):
    """Dotted IPv4 from its octets."""
    return '.'.join(str(o) for o in octets)


# Carrier-grade NAT (RFC 6598): neither private nor reserved to ipaddress,
# and the range Tailscale hands out.
_CGNAT = _v4(100, 100, 100, 100)
_CGNAT_LOW = _v4(100, 64, 0, 1)


def _addrs(*ips):
    """Build a getaddrinfo-shaped result list for the given IP literals."""
    out = []
    for ip in ips:
        if ':' in ip:
            family = socket.AF_INET6
            sockaddr = (ip, 0, 0, 0)
        else:
            family = socket.AF_INET
            sockaddr = (ip, 0)
        out.append((family, socket.SOCK_STREAM, 0, '', sockaddr))
    return out


# ── Pre-DNS rejections ───────────────────────────────────────────────

class TestPreResolutionRejection:
    """URLs we reject before ever calling socket.getaddrinfo."""

    def test_rejects_bare_rfc1918_ipv4_192(self):
        ok, reason = validate_outbound_url('https://192.168.0.1/admin')
        assert ok is False
        assert 'IP addresses not allowed' in reason

    def test_rejects_bare_rfc1918_ipv4_10(self):
        ok, reason = validate_outbound_url('https://10.0.0.1/')
        assert ok is False
        assert 'IP addresses not allowed' in reason

    def test_rejects_bare_rfc1918_ipv4_172(self):
        ok, reason = validate_outbound_url('https://172.16.0.1/')
        assert ok is False
        assert 'IP addresses not allowed' in reason

    def test_rejects_aws_metadata_literal(self):
        ok, reason = validate_outbound_url(
            'https://169.254.169.254/latest/meta-data/'
        )
        assert ok is False
        assert 'IP addresses not allowed' in reason

    def test_rejects_ipv4_loopback_literal(self):
        ok, reason = validate_outbound_url('https://127.0.0.1/')
        assert ok is False
        assert 'IP addresses not allowed' in reason

    def test_rejects_localhost_hostname(self):
        ok, reason = validate_outbound_url('https://localhost:8000/')
        assert ok is False
        assert 'Internal hostname blocked' in reason

    def test_rejects_dotinternal_suffix(self):
        ok, reason = validate_outbound_url('https://api.internal/secret')
        assert ok is False
        assert 'Internal hostname blocked' in reason

    def test_rejects_dotlocal_suffix(self):
        ok, reason = validate_outbound_url('https://router.local/')
        assert ok is False
        assert 'Internal hostname blocked' in reason

    def test_rejects_http_scheme(self):
        ok, reason = validate_outbound_url('http://ntfy.sh/topic')
        assert ok is False
        assert 'HTTPS' in reason

    def test_rejects_empty(self):
        ok, reason = validate_outbound_url('')
        assert ok is False
        assert 'required' in reason.lower()

    def test_rejects_overlong(self):
        url = 'https://example.com/' + ('a' * 4000)
        ok, reason = validate_outbound_url(url)
        assert ok is False
        assert 'too long' in reason.lower()

    def test_rejects_no_hostname(self):
        ok, reason = validate_outbound_url('https:///path')
        assert ok is False
        # urlparse may yield empty hostname or fail entirely
        assert 'hostname' in reason.lower() or 'invalid' in reason.lower()


# ── DNS-resolution rejections ────────────────────────────────────────

class TestResolvedAddressRejection:
    """URLs whose hostname resolves to an unsafe address."""

    def test_rejects_internal_dns_name(self):
        """bugsink:8000 — Docker-internal DNS resolves to a private IP."""
        with patch('socket.getaddrinfo',
                   return_value=_addrs('172.18.0.5')):
            ok, reason = validate_outbound_url('https://bugsink:8000/')
        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()

    def test_rejects_resolved_loopback(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('127.0.0.1')):
            ok, reason = validate_outbound_url('https://attacker.example/')
        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()

    def test_rejects_resolved_link_local(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('169.254.169.254')):
            ok, reason = validate_outbound_url('https://attacker.example/')
        assert ok is False
        assert 'metadata' in reason.lower() or 'private' in reason.lower()

    def test_rejects_resolved_ipv6_ula(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('fd00::1')):
            ok, reason = validate_outbound_url('https://attacker.example/')
        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()

    @pytest.mark.parametrize('ip', [
        _CGNAT,
        _CGNAT_LOW,         # bottom of the RFC 6598 block
        '198.18.0.1',       # benchmarking range
        '192.0.0.8',        # IETF protocol assignments
    ])
    def test_rejects_resolved_non_global_ipv4(self, ip):
        """Only globally routable addresses pass, not merely non-private."""
        with patch('socket.getaddrinfo', return_value=_addrs(ip)):
            ok, reason = validate_outbound_url('https://attacker.example/')
        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()

    @pytest.mark.parametrize('ip', [
        f'::ffff:{_CGNAT}',        # IPv4-mapped
        '64:ff9b::a00:5',          # NAT64 well-known prefix for 10.0.0.5
        '64:ff9b::6464:6464',      # NAT64 for the carrier-grade NAT address
    ])
    def test_rejects_ipv6_wrapping_an_internal_ipv4(self, ip):
        with patch('socket.getaddrinfo', return_value=_addrs(ip)):
            ok, reason = validate_outbound_url('https://attacker.example/')
        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()

    def test_rejects_when_any_resolved_address_is_internal(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('93.184.216.34', _CGNAT)):
            ok, _ = validate_outbound_url('https://attacker.example/')
        assert ok is False

    def test_rejects_cgnat_ip_literal(self):
        ok, reason = validate_outbound_url(f'https://{_CGNAT}/')
        assert ok is False
        assert 'IP addresses not allowed' in reason

    def test_rejects_resolution_failure(self):
        with patch('socket.getaddrinfo',
                   side_effect=socket.gaierror('nope')):
            ok, reason = validate_outbound_url('https://nx.example.invalid/')
        assert ok is False
        assert 'resolve' in reason.lower()


# ── Allowlist enforcement ────────────────────────────────────────────

class TestAllowlist:
    """Hostname must match the caller-supplied set when given."""

    def test_rejects_host_not_in_allowlist(self):
        ok, reason = validate_outbound_url(
            'https://example.com/',
            allow_hosts=('ntfy.sh',),
        )
        assert ok is False
        assert 'allowlist' in reason.lower()

    def test_accepts_ntfy_with_allowlist(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('1.2.3.4')):
            ok, info = validate_outbound_url(
                'https://ntfy.sh/my-topic',
                allow_hosts=('ntfy.sh',),
            )
        assert ok is True
        assert info == '1.2.3.4'

    def test_allowlist_with_self_hosted_extra(self):
        """Operator-supplied extra host (NTFY_BASE_HOST pattern)."""
        with patch('socket.getaddrinfo',
                   return_value=_addrs('8.8.8.8')):
            ok, info = validate_outbound_url(
                'https://ntfy.example.com/topic',
                allow_hosts=('ntfy.sh', 'ntfy.example.com'),
            )
        assert ok is True
        assert info == '8.8.8.8'

    def test_allowlist_case_insensitive(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('1.2.3.4')):
            ok, _ = validate_outbound_url(
                'https://NTFY.SH/Foo',
                allow_hosts=('ntfy.sh',),
            )
        assert ok is True


# ── Happy path + DNS rebind ──────────────────────────────────────────

class TestHappyPathAndRebind:

    def test_accepts_public_ipv6(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('2606:2800:220:1:248:1893:25c8:1946')):
            ok, info = validate_outbound_url('https://example.com/')
        assert ok is True
        assert info == '2606:2800:220:1:248:1893:25c8:1946'

    def test_accepts_public_https(self):
        with patch('socket.getaddrinfo',
                   return_value=_addrs('93.184.216.34')):
            ok, info = validate_outbound_url('https://example.com/')
        assert ok is True
        assert info == '93.184.216.34'

    def test_dns_rebind_first_resolution_wins(self):
        """If the FIRST getaddrinfo returns a private IP, we reject —
        even if later lookups would return a public IP. We resolve once
        and pin so the caller's dialer doesn't get rebound between
        validation and request.
        """
        call_count = {'n': 0}

        def fake_getaddrinfo(*a, **kw):
            call_count['n'] += 1
            if call_count['n'] == 1:
                return _addrs('10.0.0.5')   # private at validation time
            return _addrs('93.184.216.34')  # public at request time

        with patch('socket.getaddrinfo', side_effect=fake_getaddrinfo):
            ok, reason = validate_outbound_url('https://rebind.example/')
        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()
        # validate_outbound_url must NOT have called getaddrinfo a
        # second time — we trust the first answer.
        assert call_count['n'] == 1

    def test_returned_pinned_ip_can_be_used_by_caller(self):
        """The returned IP literal lets the caller defeat rebind by
        dialling the IP directly + sending Host: hostname."""
        with patch('socket.getaddrinfo',
                   return_value=_addrs('1.1.1.1')):
            ok, pinned = validate_outbound_url('https://example.com/')
        assert ok is True
        # caller can now: requests.get(f'https://{pinned}/...',
        #                              headers={'Host': 'example.com'})
        assert pinned == '1.1.1.1'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
