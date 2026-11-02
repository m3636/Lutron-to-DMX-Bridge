"""LEAP pairing handshake (button press -> client certificate).

This follows ``pylutron_caseta.pairing.async_pair`` step for step, with one
addition: the controller's certificate on the pairing port (8083) is checked
against Lutron's published CAs first, and only if neither matches AND the caller
passed ``trust_presented_cert=True`` does it accept whatever certificate the
controller presents. Without that opt-in, pairing stops with
:class:`UntrustedControllerCert`, which carries the certificate's details so the
user can decide.

Why the fallback exists: some processors (seen on a real RA3/RA2 Select system)
present a pairing certificate that chains to neither the Caseta CA nor the
Lutron root CA bundled with pylutron_caseta, and the library then fails with
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``.

The relaxed mode is an explicit user decision and is limited to the pairing socket, where nothing secret is sent
(the client only presents Lutron's public pairing certificate and a CSR) and
where a physical button press on the controller is required. The pairing reply
carries the controller's own root CA; the final step, and every later
connection (:class:`~lutron_dmx.drivers.leap.LeapDriver`), verifies the server
against that CA, so verification is never disabled for normal operation. The
trade-off is trust-on-first-use: someone who can intercept the LAN during the
few seconds of pairing could hand out their own root.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import ssl
import tempfile
from collections.abc import Callable
from datetime import timezone
from pathlib import Path
from typing import Any

import orjson
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pylutron_caseta.assets import LAP_CA_PEM, LAP_CERT_PEM, LAP_KEY_PEM, LUTRON_ROOT_CA_PEM

from .base import DriverError

log = logging.getLogger(__name__)

PAIRING_PORT = 8083
LEAP_PORT = 8081
SOCKET_TIMEOUT = 10.0
BUTTON_PRESS_TIMEOUT = 180.0
CERT_COMMON_NAME = "lutron-dmx"

#: (label, CA bundle) tried in order for the pairing socket.
TRUST_STAGES: list[tuple[str, str | None]] = [
    ("Lutron Caseta CA", LAP_CA_PEM),
    ("Lutron root CA", LUTRON_ROOT_CA_PEM),
]
#: Added after TRUST_STAGES only when the user opted in. ``None`` = accept the
#: controller's certificate unverified (pairing socket only).
UNVERIFIED_STAGE: tuple[str, str | None] = ("certificate presented by the controller (unverified)", None)


class UntrustedControllerCert(DriverError):
    """The pairing socket's certificate chains to no known Lutron CA; ``details`` describes it."""

    def __init__(self, host: str, port: int, failures: list[str], details: str) -> None:
        self.details = details
        super().__init__(
            f"The controller at {host}:{port} presented a certificate that does not chain to a "
            f"known Lutron CA ({'; '.join(failures)}). It presented: {details}. "
            "If this is your controller, pair again and choose to trust it for this one pairing "
            "(CLI: --trust-controller-cert).")


def describe_cert(der: bytes) -> str:
    """One-line subject / issuer / validity / SHA-256 summary of a DER certificate."""
    try:
        cert = x509.load_der_x509_certificate(der)
        nb = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") \
            else cert.not_valid_before.replace(tzinfo=timezone.utc)
        na = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") \
            else cert.not_valid_after.replace(tzinfo=timezone.utc)
        return (f"subject={cert.subject.rfc4514_string()!r} issuer={cert.issuer.rfc4514_string()!r} "
                f"valid={nb:%Y-%m-%d}..{na:%Y-%m-%d} sha256={hashlib.sha256(der).hexdigest()}")
    except Exception as exc:  # diagnostics must never break pairing
        return f"unparseable certificate ({exc}); sha256={hashlib.sha256(der).hexdigest()}"


def _client_context(cafile_pem: str | None, cert_pem: str | None = None,
                    key_pem: str | bytes | None = None) -> ssl.SSLContext:
    """TLS 1.2 client context with an optional client certificate; hostname is never checked
    (LEAP servers are addressed by IP and present no matching name)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    if cafile_pem is None:
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.load_verify_locations(cadata=cafile_pem)
        ctx.verify_mode = ssl.CERT_REQUIRED
    if cert_pem is not None and key_pem is not None:
        key_bytes = key_pem.encode("ascii") if isinstance(key_pem, str) else key_pem
        # load_cert_chain only takes paths, so go through short-lived temp files
        with tempfile.TemporaryDirectory() as d:
            cert_path, key_path = Path(d, "c.pem"), Path(d, "k.pem")
            cert_path.write_text(cert_pem, encoding="ascii")
            key_path.write_bytes(key_bytes)
            ctx.load_cert_chain(cert_path, key_path)
    return ctx


def _generate_key_and_csr() -> tuple[bytes, x509.CertificateSigningRequest]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_COMMON_NAME)]))
           .sign(key, hashes.SHA256()))
    return pem, csr


class _JsonSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer

    async def read(self, timeout: float) -> Any:
        line = await asyncio.wait_for(self._reader.readline(), timeout)
        if not line:
            raise DriverError("The controller closed the connection during pairing")
        log.debug("pairing received: %s", line)
        return orjson.loads(line)

    def write(self, obj: Any) -> None:
        data = orjson.dumps(obj)
        self._writer.writelines((data, b"\r\n"))
        log.debug("pairing sent: %s", data)

    def close(self) -> None:
        self._writer.close()


async def _open(host: str, port: int, ctx: ssl.SSLContext) -> _JsonSocket:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx, server_hostname="", family=socket.AF_INET),
        SOCKET_TIMEOUT)
    return _JsonSocket(reader, writer)


async def _peek_certificate(host: str, port: int) -> str:
    """Describe the certificate a server presents. Sends no data; it presents Lutron's public
    pairing certificate, because processors refuse a handshake without a client certificate."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=_client_context(None, LAP_CERT_PEM, LAP_KEY_PEM), server_hostname="",
                                    family=socket.AF_INET), SOCKET_TIMEOUT)
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
        return f"(could not read it: {exc or type(exc).__name__})"
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return describe_cert(der) if der else "(no certificate)"
    finally:
        writer.close()


async def _connect_for_pairing(host: str, port: int, client_ctx_factory: Callable[[str | None], ssl.SSLContext],
                               trust_presented_cert: bool = False
                               ) -> tuple[_JsonSocket, str, str | None]:
    """Open the pairing socket, walking the trust stages. Returns (socket, stage label, CA used)."""
    failures: list[str] = []
    stages = TRUST_STAGES + ([UNVERIFIED_STAGE] if trust_presented_cert else [])
    for label, ca in stages:
        try:
            sock = await _open(host, port, client_ctx_factory(ca))
        except ssl.SSLCertVerificationError as exc:
            log.info("Pairing socket rejected by %s: %s", label, exc.verify_message)
            failures.append(f"{label}: {exc.verify_message}")
            continue
        except ssl.SSLError as exc:
            raise DriverError(f"TLS error talking to {host}:{port}: {exc}") from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise DriverError(f"Could not connect to {host}:{port}: {exc or type(exc).__name__}") from exc
        if ca is None:
            writer_ssl = sock._writer.get_extra_info("ssl_object")
            der = writer_ssl.getpeercert(binary_form=True) if writer_ssl else None
            log.warning("Lutron CAs did not match; pairing with unverified certificate: %s",
                        describe_cert(der) if der else "(none)")
        return sock, label, ca
    raise UntrustedControllerCert(host, port, failures, await _peek_certificate(host, port))


async def async_pair(host: str, ready: Callable[[], None] | None = None, *,
                     trust_presented_cert: bool = False, pairing_port: int = PAIRING_PORT, leap_port: int = LEAP_PORT,
                     button_timeout: float = BUTTON_PRESS_TIMEOUT) -> dict[str, str]:
    """Pair with a LEAP processor. Returns ``{"key", "cert", "ca", "version"}`` (PEM strings).

    Raises :class:`UntrustedControllerCert` if the pairing certificate matches no Lutron CA
    and ``trust_presented_cert`` is false."""
    loop = asyncio.get_running_loop()
    key_pem, csr = await loop.run_in_executor(None, _generate_key_and_csr)

    def pairing_ctx(ca: str | None) -> ssl.SSLContext:
        return _client_context(ca, LAP_CERT_PEM, LAP_KEY_PEM)

    sock, stage, trusted_ca = await _connect_for_pairing(host, pairing_port, pairing_ctx, trust_presented_cert)
    log.info("Pairing socket open (trust: %s)", stage)
    try:
        if ready is not None:
            ready()
        while True:
            msg = await sock.read(button_timeout)
            header, body = msg.get("Header", {}), msg.get("Body", {})
            if header.get("ContentType", "").startswith("status;") and \
                    "PhysicalAccess" in body.get("Status", {}).get("Permissions", []):
                break
        sock.write({
            "Header": {"RequestType": "Execute", "Url": "/pair", "ClientTag": "get-cert"},
            "Body": {"CommandType": "CSR", "Parameters": {
                "CSR": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
                "DisplayName": CERT_COMMON_NAME, "DeviceUID": "000000000000", "Role": "Admin"}},
        })
        while True:
            msg = await sock.read(SOCKET_TIMEOUT)
            if msg.get("Header", {}).get("ClientTag") == "get-cert":
                break
    except asyncio.TimeoutError as exc:
        raise DriverError("Timed out waiting for the button press on the controller") from exc
    finally:
        sock.close()

    try:
        signing = msg["Body"]["SigningResult"]
        cert_pem, root_pem = signing["Certificate"], signing["RootCertificate"]
    except (KeyError, TypeError) as exc:
        raise DriverError(f"Unexpected pairing reply from the controller: {msg}") from exc

    # Verify the paired credentials against the CA the controller gave us (or, as
    # the library does for RA3, the Lutron root). This is the check later connections use.
    candidates = [root_pem]
    if trusted_ca == LUTRON_ROOT_CA_PEM:
        candidates = [LUTRON_ROOT_CA_PEM]
    elif trusted_ca is None and LUTRON_ROOT_CA_PEM not in candidates:
        candidates.append(LUTRON_ROOT_CA_PEM)
    last: Exception | None = None
    for ca_pem in candidates:
        ctx = await loop.run_in_executor(None, _client_context, ca_pem, cert_pem, key_pem)
        try:
            leap = await _open(host, leap_port, ctx)
        except ssl.SSLCertVerificationError as exc:
            last = exc
            log.info("LEAP port %d certificate did not verify against a candidate CA: %s",
                     leap_port, exc.verify_message)
            continue
        except (ssl.SSLError, OSError, asyncio.TimeoutError) as exc:
            raise DriverError(f"Paired, but the test connection to {host}:{leap_port} failed: "
                              f"{exc or type(exc).__name__}") from exc
        try:
            leap.write({"CommuniqueType": "ReadRequest", "Header": {"Url": "/server/1/status/ping"}})
            while True:
                reply = await leap.read(SOCKET_TIMEOUT)
                if reply.get("CommuniqueType") == "ReadResponse":
                    break
        except asyncio.TimeoutError as exc:
            raise DriverError("Paired, but the controller did not answer a LEAP ping") from exc
        finally:
            leap.close()
        version = reply.get("Body", {}).get("PingResponse", {}).get("LEAPVersion", "")
        log.info("Paired; controller speaks LEAP %s", version)
        return {"key": key_pem.decode("ascii"), "cert": cert_pem, "ca": ca_pem, "version": version}
    raise DriverError(f"Paired, but the controller's LEAP certificate on port {leap_port} did not "
                      f"verify against the CA it issued ({last}). Re-run with -v and send the output.")
