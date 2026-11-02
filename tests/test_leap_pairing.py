"""LEAP pairing handshake against a fake controller (no hardware).

The fake controller uses its own private CA, i.e. one that is NOT among the Lutron
CAs bundled with pylutron_caseta - the situation that produced
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`` on real hardware.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pylutron_caseta.assets import LAP_CA_PEM

from lutron_to_dmx_bridge.drivers import DriverError, leap, leap_pairing
from lutron_to_dmx_bridge.drivers.leap_pairing import UntrustedControllerCert, async_pair, describe_cert

NOW = datetime.datetime.now(datetime.timezone.utc)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class Ca:
    """A tiny CA that can issue server certificates and sign CSRs."""

    def __init__(self, cn: str) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.cert = (self._builder(_name(cn), _name(cn), self.key.public_key())
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .sign(self.key, hashes.SHA256()))
        self.pem = _pem(self.cert)

    def _builder(self, subject, issuer, public_key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(NOW - datetime.timedelta(days=1))
                .not_valid_after(NOW + datetime.timedelta(days=30)))

    def sign_pub(self, cn: str, public_key) -> x509.Certificate:
        return self._builder(_name(cn), self.cert.subject, public_key).sign(self.key, hashes.SHA256())

    def server_context(self, tmp_path: Path, cn: str = "controller",
                       require_client_cert: bool = False) -> ssl.SSLContext:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = self.sign_pub(cn, key.public_key())
        cert_file, key_file = tmp_path / f"{cn}-{id(self)}.crt", tmp_path / f"{cn}-{id(self)}.key"
        cert_file.write_text(_pem(cert))
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        if require_client_cert:  # like a real processor: no client certificate, no handshake
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=LAP_CA_PEM)
        return ctx


async def _send(writer, obj):
    writer.write(json.dumps(obj).encode() + b"\r\n")
    await writer.drain()


def _pairing_handler(ca: Ca, press_button: bool = True):
    async def handle(reader, writer):
        try:
            if press_button:
                await _send(writer, {"Header": {"ContentType": "status;plurality=single"},
                                     "Body": {"Status": {"Permissions": ["PhysicalAccess"]}}})
            req = json.loads(await reader.readline())
            csr = x509.load_pem_x509_csr(req["Body"]["Parameters"]["CSR"].encode())
            await _send(writer, {"Header": {"ClientTag": req["Header"]["ClientTag"]},
                                 "Body": {"SigningResult": {
                                     "Certificate": _pem(ca.sign_pub("lutron-to-dmx-bridge", csr.public_key())),
                                     "RootCertificate": ca.pem}}})
            await asyncio.sleep(0.1)
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
    return handle


def _leap_handler():
    async def handle(reader, writer):
        try:
            await reader.readline()
            await _send(writer, {"CommuniqueType": "ReadResponse",
                                 "Body": {"PingResponse": {"LEAPVersion": 1.115}}})
            await asyncio.sleep(0.1)
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
    return handle


async def _serve(handler, ctx):
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
def private_ca():
    return Ca("Some Other Lutron Root")


async def _pair(pairing_port, leap_port, **kw):
    return await async_pair("127.0.0.1", pairing_port=pairing_port, leap_port=leap_port,
                            button_timeout=5, **kw)


async def test_unknown_ca_is_refused_by_default(tmp_path, private_ca):
    """An unknown issuer on the pairing port stops pairing and reports what was presented,
    even when the controller demands a client certificate (as a real RA3 does)."""
    ps, pp = await _serve(_pairing_handler(private_ca),
                          private_ca.server_context(tmp_path, require_client_cert=True))
    ls, lp = await _serve(_leap_handler(), private_ca.server_context(tmp_path))
    async with ps, ls:
        with pytest.raises(UntrustedControllerCert) as ei:
            await _pair(pp, lp)
    assert "Some Other Lutron Root" in str(ei.value) and "sha256=" in ei.value.details
    assert "--trust-controller-cert" in str(ei.value)


async def test_unknown_ca_pairs_when_user_opts_in(tmp_path, private_ca, caplog):
    ps, pp = await _serve(_pairing_handler(private_ca), private_ca.server_context(tmp_path))
    ls, lp = await _serve(_leap_handler(), private_ca.server_context(tmp_path))
    async with ps, ls:
        with caplog.at_level("INFO", logger="lutron_to_dmx_bridge.drivers.leap_pairing"):
            data = await _pair(pp, lp, trust_presented_cert=True)
    assert data["ca"] == private_ca.pem
    assert "BEGIN CERTIFICATE" in data["cert"] and "PRIVATE KEY" in data["key"]
    assert data["version"] == "1.115" or data["version"] == 1.115
    assert "unverified" in caplog.text  # the relaxed stage was used and logged


async def test_prefers_verified_stage_over_unverified(tmp_path, private_ca, monkeypatch, caplog):
    monkeypatch.setattr(leap_pairing, "TRUST_STAGES", [("test CA", private_ca.pem)])
    ps, pp = await _serve(_pairing_handler(private_ca), private_ca.server_context(tmp_path))
    ls, lp = await _serve(_leap_handler(), private_ca.server_context(tmp_path))
    async with ps, ls:
        with caplog.at_level("INFO", logger="lutron_to_dmx_bridge.drivers.leap_pairing"):
            await _pair(pp, lp, trust_presented_cert=True)
    assert "trust: test CA" in caplog.text and "unverified" not in caplog.text


async def test_leap_port_is_still_verified(tmp_path, private_ca):
    """The relaxed trust applies to the pairing socket only: a LEAP server whose certificate is not
    issued by the CA the controller handed out must be rejected."""
    impostor = Ca("Impostor")
    ps, pp = await _serve(_pairing_handler(private_ca), private_ca.server_context(tmp_path))
    ls, lp = await _serve(_leap_handler(), impostor.server_context(tmp_path))
    async with ps, ls:
        with pytest.raises(DriverError, match="did not verify"):
            await _pair(pp, lp, trust_presented_cert=True)


async def test_button_not_pressed_times_out(tmp_path, private_ca):
    ps, pp = await _serve(_pairing_handler(private_ca, press_button=False),
                          private_ca.server_context(tmp_path))

    async def never(reader, writer):
        await asyncio.sleep(5)

    async with ps:
        with pytest.raises((DriverError, asyncio.TimeoutError)):
            await asyncio.wait_for(
                async_pair("127.0.0.1", pairing_port=pp, leap_port=1, button_timeout=0.5), 5)


async def test_unreachable_controller_is_a_clear_error():
    with pytest.raises(DriverError, match="Could not connect"):
        await async_pair("127.0.0.1", pairing_port=1, leap_port=1)


def test_describe_cert_summarises_issuer(private_ca):
    text = describe_cert(private_ca.cert.public_bytes(serialization.Encoding.DER))
    assert "Some Other Lutron Root" in text and "sha256=" in text
    assert "unparseable" in describe_cert(b"not a certificate")


async def test_pair_wraps_errors(tmp_path, monkeypatch):
    async def boom(host, **kw):
        raise ssl.SSLError("nope")

    monkeypatch.setattr(leap, "async_pair", boom)
    with pytest.raises(DriverError, match="Pairing with 10.0.0.1 failed"):
        await leap.pair("10.0.0.1", tmp_path)

