"""Discovery tests: no Lutron hardware, only local fake servers and text fixtures."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
import ssl
from types import SimpleNamespace

import pytest

from lutron_dmx import discovery
from lutron_dmx.discovery import arp, mdns, netinfo, scan
from lutron_dmx.discovery.models import DiscoveredDevice


# ---------------------------------------------------------------- ARP parsing
WINDOWS_ARP = """
Interface: 192.168.1.5 --- 0x7
  Internet Address      Physical Address      Type
  192.168.1.1           a4-2b-b0-11-22-33     dynamic
  192.168.1.60          00-0f-e7-01-02-03     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""

MAC_ARP = """? (192.168.1.1) at a4:2b:b0:11:22:33 on en0 ifscope [ethernet]
? (192.168.1.60) at 0:f:e7:1:2:3 on en0 ifscope [ethernet]
? (192.168.1.77) at (incomplete) on en0 ifscope [ethernet]
? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
"""

LINUX_ARP = "? (192.168.1.60) at 00:0f:e7:01:02:03 [ether] on eth0\n"


@pytest.mark.parametrize("text", [WINDOWS_ARP, MAC_ARP, LINUX_ARP])
def test_parse_arp_finds_lutron_mac(text):
    table = arp.parse_arp(text)
    assert table["192.168.1.60"] == "00:0f:e7:01:02:03"
    assert arp.is_lutron_oui(table["192.168.1.60"])
    assert not any(ip.endswith(".255") or ip.startswith("224.") for ip in table)


def test_parse_arp_skips_incomplete_and_others():
    table = arp.parse_arp(MAC_ARP)
    assert "192.168.1.77" not in table
    assert not arp.is_lutron_oui(table["192.168.1.1"])


# ---------------------------------------------------------------- network selection
def _adapter(name, *ips):
    return SimpleNamespace(
        nice_name=name, name=name,
        ips=[SimpleNamespace(ip=ip, network_prefix=prefix) for ip, prefix in ips])


def test_list_local_networks_filters_and_trims():
    adapters = [
        _adapter("Ethernet", ("192.168.1.5", 24), (("fe80::1", 0, 0), 64)),
        _adapter("vEthernet (WSL)", ("172.20.0.1", 20)),
        _adapter("Wi-Fi", ("10.1.2.3", 16)),  # bigger than /24: trimmed to the /24 around us
        _adapter("lo", ("127.0.0.1", 8)),
        _adapter("en9", ("169.254.3.4", 16)),
        _adapter("en1", ("8.8.8.8", 24)),  # not private
        _adapter("Ethernet 2", ("192.168.1.9", 24)),  # same network as the first: deduplicated
    ]
    nets = netinfo.list_local_networks(lambda: adapters)
    assert [str(n.network) for n in nets] == ["192.168.1.0/24", "10.1.2.0/24"]
    assert netinfo.mdns_interface_addresses(nets) == ["10.1.2.3", "192.168.1.5"]


def test_candidate_hosts_skips_own_address_and_caps():
    nets = netinfo.list_local_networks(lambda: [_adapter("Ethernet", ("192.168.1.5", 24))])
    hosts = scan.candidate_hosts(nets)
    assert "192.168.1.5" not in hosts and len(hosts) == 253
    assert len(scan.candidate_hosts([ipaddress.ip_network("10.0.0.0/16")], max_hosts=100)) == 100


# ---------------------------------------------------------------- mDNS
def test_device_from_service_radiora3():
    dev = mdns.device_from_service(
        "Lutron-01234ABC._lutron._tcp.local.", ["192.168.1.60", "fe80::1"], 8081,
        {b"SYSTYPE": b"RadioRA3-Processor"})
    assert dev.host == "192.168.1.60" and dev.port == 8081 and dev.kind == "leap"
    assert dev.system == "RadioRA 3" and dev.name == "lutron-01234abc"
    assert dev.confidence == 1.0


def test_device_from_service_unknown_and_ipv6_only():
    assert mdns.device_from_service("Lutron-1._lutron._tcp.local.", ["fe80::1"], 8081, {}) is None
    dev = mdns.device_from_service("something._lutron._tcp.local.", ["10.0.0.9"], None, {})
    assert dev.port == 8081 and dev.confidence == 0.6 and dev.system == ""


@pytest.mark.parametrize("systype,label", [
    ("smartbridge2", "Caseta Smart Bridge"), ("ra2select", "RadioRA 2 Select"),
    ("hwqsx", "HomeWorks QS"), ("nope", "")])
def test_system_from_systype(systype, label):
    assert mdns.system_from_systype(systype) == label


async def test_mdns_roundtrip_with_fake_service():
    """Advertise a fake processor with python-zeroconf and find it again (needs local multicast)."""
    from zeroconf import IPVersion, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf

    ip = None
    for n in netinfo.list_local_networks():
        ip = str(n.address)
        break
    if ip is None:
        pytest.skip("no private IPv4 interface for multicast")
    adv = AsyncZeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
    info = ServiceInfo(
        "_lutron._tcp.local.", "Lutron-0DEADBEE._lutron._tcp.local.",
        addresses=[socket.inet_aton(ip)], port=8081,
        properties={"SYSTYPE": "RadioRA3-test"}, server="lutron-0deadbee.local.")
    try:
        await adv.async_register_service(info)
        found = await mdns.discover_mdns(timeout=5.0, interfaces=[ip])
    except OSError as exc:
        pytest.skip(f"multicast unavailable: {exc}")
    finally:
        await adv.async_close()
    if not found:
        pytest.skip("multicast loopback not delivered on this host")
    assert found[0].host == ip and found[0].system == "RadioRA 3"
    assert found[0].name == "lutron-0deadbee"


# ---------------------------------------------------------------- scan (fake servers on 127.0.0.1)
LOCAL = [ipaddress.ip_network("127.0.0.1/32")]


async def _telnet_server(banner: bytes):
    async def handle(reader, writer):
        writer.write(banner)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_scan_finds_ra2_repeater():
    server, port = await _telnet_server(b"\xff\xfb\x01login: ")
    async with server:
        found = await scan.scan_subnets(LOCAL, ports=(port,), timeout=1.0,
                                        probes={port: scan.probe_ra2})
    assert [(d.host, d.port, d.kind) for d in found] == [("127.0.0.1", port, "ra2")]
    assert found[0].system == "RadioRA 2 Main Repeater"


async def test_scan_ignores_other_telnet_and_closed_ports():
    server, port = await _telnet_server(b"Welcome to my router\r\n# ")
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    async with server:
        found = await scan.scan_subnets(
            LOCAL, ports=(port, closed_port), timeout=0.5,
            probes={port: scan.probe_ra2, closed_port: scan.probe_ra2})
    assert found == []


def _self_signed(org: str, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
                      x509.NameAttribute(NameOID.COMMON_NAME, "processor")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / f"{org}.pem", tmp_path / f"{org}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_file, key_file


@pytest.mark.parametrize("org,kind_conf", [("Lutron Electronics Co., Inc.", 0.9), ("Some Other Corp", 0.4)])
async def test_scan_leap_fingerprint(tmp_path, org, kind_conf):
    pytest.importorskip("cryptography")
    cert_file, key_file = _self_signed(org, tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)

    async def handle(reader, writer):
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    async with server:
        found = await scan.scan_subnets(LOCAL, ports=(port,), timeout=1.0,
                                        probes={port: scan.probe_leap})
    assert len(found) == 1 and found[0].kind == "leap" and found[0].confidence == kind_conf


async def test_scan_reports_os_blocking():
    async def blocked(host, port, timeout):
        raise OSError(65, "No route to host")  # EHOSTUNREACH on macOS

    with pytest.raises(scan.ScanBlocked):
        await scan.scan_subnets([ipaddress.ip_network("10.9.9.0/30")], ports=(8081,),
                                probes={8081: blocked})


# ---------------------------------------------------------------- orchestration
def _dev(host, method="mdns", kind="leap", conf=0.9, **kw):
    return DiscoveredDevice(host=host, port=8081, kind=kind, method=method, confidence=conf, **kw)


async def test_discover_skips_scan_when_mdns_finds_something():
    calls = []

    async def fake_mdns(**_):
        return [_dev("10.0.0.2", system="RadioRA 3", name="lutron-1")]

    async def fake_scan(**_):
        calls.append(1)
        return []

    async def fake_arp():
        return {"10.0.0.2": "00:0f:e7:00:00:01"}

    out = await discovery.discover(mdns_fn=fake_mdns, scan_fn=fake_scan, arp_fn=fake_arp)
    assert calls == [] and len(out) == 1
    assert out[0].mac == "00:0f:e7:00:00:01" and out[0].confidence == 1.0


async def test_discover_falls_back_to_scan_and_merges():
    async def fake_mdns(**_):
        return []

    async def fake_scan(**_):
        return [_dev("10.0.0.7", "scan", "ra2", 0.85), _dev("10.0.0.8", "scan", conf=0.4)]

    async def fake_arp():
        return {}

    out = await discovery.discover(mdns_fn=fake_mdns, scan_fn=fake_scan, arp_fn=fake_arp)
    assert [d.host for d in out] == ["10.0.0.7", "10.0.0.8"]  # best match first


async def test_discover_scan_true_merges_same_host():
    async def fake_mdns(**_):
        return [_dev("10.0.0.2", name="lutron-1", system="RadioRA 3")]

    async def fake_scan(**_):
        return [_dev("10.0.0.2", "scan", conf=0.9)]

    async def fake_arp():
        return {}

    out = await discovery.discover(scan=True, mdns_fn=fake_mdns, scan_fn=fake_scan, arp_fn=fake_arp)
    assert len(out) == 1 and out[0].method == "mdns+scan" and out[0].name == "lutron-1"


async def test_discover_survives_mdns_failure_and_surfaces_blocked_scan():
    async def broken_mdns(**_):
        raise RuntimeError("no multicast")

    async def blocked_scan(**_):
        raise scan.ScanBlocked("blocked by OS")

    with pytest.raises(scan.ScanBlocked):
        await discovery.discover(mdns_fn=broken_mdns, scan_fn=blocked_scan, arp_fn=lambda: {})


async def test_rediscover_host_by_name():
    async def fake_mdns(**_):
        return [_dev("10.0.0.50", name="lutron-abc"), _dev("10.0.0.51", name="lutron-other")]

    assert await discovery.rediscover_host("Lutron-ABC", mdns_fn=fake_mdns) == "10.0.0.50"
    assert await discovery.rediscover_host("lutron-zzz", mdns_fn=fake_mdns) is None
    assert await discovery.rediscover_host("", mdns_fn=fake_mdns) is None


# ---------------------------------------------------------------- controller re-finds a moved processor
async def test_controller_rediscovers_moved_processor(tmp_path, monkeypatch):
    from lutron_dmx import controller as controller_mod
    from lutron_dmx.config import AppConfig, DriverConfig
    from lutron_dmx.controller import Controller
    from lutron_dmx.drivers.base import DriverError

    monkeypatch.setenv("LUTRON_DMX_HOME", str(tmp_path))

    class Driver:
        def __init__(self, cfg):
            self.host = cfg.host

        async def connect(self):
            if self.host != "10.0.0.50":
                raise DriverError("connection refused")

        async def close(self):
            pass

    async def fake_rediscover(name):
        assert name == "lutron-abc"
        return "10.0.0.50"

    monkeypatch.setattr(controller_mod, "create_driver", Driver)
    monkeypatch.setattr(discovery, "rediscover_host", fake_rediscover)
    cfg = AppConfig(driver=DriverConfig(kind="leap", host="10.0.0.9", hostname="lutron-abc"))
    ctl = Controller(cfg, driver=Driver(cfg.driver))
    await ctl.start()
    assert cfg.driver.host == "10.0.0.50" and ctl.last_error is None
    assert AppConfig.load().driver.host == "10.0.0.50"  # remembered for next time


async def test_controller_does_not_rediscover_without_hostname(monkeypatch, tmp_path):
    from lutron_dmx.config import AppConfig, DriverConfig
    from lutron_dmx.controller import Controller

    monkeypatch.setenv("LUTRON_DMX_HOME", str(tmp_path))

    class Boom:
        async def connect(self):
            raise OSError("down")

        async def close(self):
            pass

    async def must_not_run(name):
        raise AssertionError("rediscovery should not run")

    monkeypatch.setattr(discovery, "rediscover_host", must_not_run)
    cfg = AppConfig(driver=DriverConfig(kind="leap", host="10.0.0.9"))
    ctl = Controller(cfg, driver=Boom())
    await ctl.start()
    assert "down" in ctl.last_error


# ---------------------------------------------------------------- CLI
def test_cli_discover_prints_table(monkeypatch, capsys):
    from lutron_dmx import __main__ as cli

    async def fake(**_):
        return [_dev("10.0.0.60", system="RadioRA 3", name="lutron-abc", conf=1.0)]

    monkeypatch.setattr(discovery, "discover", fake)
    cli.main(["discover"])
    out = capsys.readouterr().out
    assert "10.0.0.60" in out and "RadioRA 3" in out and "lutron-dmx pair 10.0.0.60" in out


def test_cli_discover_nothing_found_exits_nonzero(monkeypatch, capsys):
    from lutron_dmx import __main__ as cli

    async def fake(**_):
        return []

    monkeypatch.setattr(discovery, "discover", fake)
    with pytest.raises(SystemExit) as exc:
        cli.main(["discover"])
    assert exc.value.code == 1
    assert "No Lutron processor found" in capsys.readouterr().out


def test_cli_pair_without_host_discovers_and_remembers_name(monkeypatch, tmp_path):
    from lutron_dmx import __main__ as cli
    from lutron_dmx.config import AppConfig
    from lutron_dmx.drivers import leap

    monkeypatch.setenv("LUTRON_DMX_HOME", str(tmp_path))
    paired = []

    async def fake_discover(**_):
        return [_dev("10.0.0.60", system="RadioRA 3", name="lutron-abc", conf=1.0),
                _dev("10.0.0.70", kind="ra2", method="scan")]

    async def fake_pair(host, out_dir, **kw):
        paired.append(host)

    monkeypatch.setattr(discovery, "discover", fake_discover)
    monkeypatch.setattr(leap, "pair", fake_pair)
    cli.main(["pair"])
    cfg = AppConfig.load()
    assert paired == ["10.0.0.60"]
    assert cfg.driver.kind == "leap" and cfg.driver.host == "10.0.0.60" and cfg.driver.hostname == "lutron-abc"
