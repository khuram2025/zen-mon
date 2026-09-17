"""Bounded, deterministic discovery targets; no network access."""
import ipaddress

from app.services.discovery_executor import expand_targets


def test_point_to_point_networks_include_both_endpoints():
    assert expand_targets(['192.0.2.0/31']) == ['192.0.2.0', '192.0.2.1']
    assert expand_targets(['2001:db8::/127']) == ['2001:db8::', '2001:db8::1']


def test_ipv6_ranges_and_canonical_deduplication():
    assert expand_targets(['2001:db8::1-2001:db8::3', '2001:0db8:0:0:0:0:0:1']) == [
        '2001:db8::1', '2001:db8::2', '2001:db8::3']


def test_zero_cap_emits_nothing():
    assert expand_targets(['192.0.2.1'], cap=0) == []


def test_large_network_expansion_is_lazy(monkeypatch):
    original = ipaddress.IPv6Network.hosts
    def guarded(self):
        for index, host in enumerate(original(self)):
            if index > 4096:
                raise AssertionError('materialized an unbounded IPv6 subnet')
            yield host
    monkeypatch.setattr(ipaddress.IPv6Network, 'hosts', guarded)
    assert len(expand_targets(['2001:db8::/64'], cap=3)) == 3


def test_exclusions_apply_to_whole_cidr_not_only_first_4096_hosts():
    assert expand_targets(['10.2.255.254'], ['10.2.0.0/16']) == []


def test_exclusion_boundaries_and_mixed_families():
    assert expand_targets(['192.0.2.0/30', '2001:db8::1'], ['192.0.2.1']) == [
        '192.0.2.2', '2001:db8::1']
    assert expand_targets(['192.0.2.1-2001:db8::1']) == []
