"""Source fetches verify TLS against the OS trust store, not certifi alone.

The case that forced it, measured 2026-09-17: www.ntia.gov sends a *complete*
chain that ends at a root Windows trusts and certifi no longer ships, so every
NTIA citation failed with "unable to get local issuer certificate". See the
certificate-verification section of ``ci_core.http``.

These tests reproduce that shape on loopback with a real TLS handshake: a
server whose leaf and intermediate chain to a throwaway root that certifi has
never heard of. Nothing leaves the machine.

What cannot be done hermetically is consulting the real OS store. Adding a root
to it would change the machine's security settings, and on Windows even
*rejecting* an unknown root makes the chain engine look for it online (~2s per
handshake, measured). So the claim is split in two, and each half is tested:

* ``_os_trust_context`` is truststore's OS verifier — asserted directly;
* the fetch path verifies with whatever that context trusts — asserted by
  substituting a context that trusts the throwaway root, standing in for an OS
  store that trusts NTIA's.
"""

import datetime
import ipaddress
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
import truststore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ci_core import http


def _name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _issue(subject, subject_key, issuer, issuer_key, *, ca, extensions=()):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    for extension in extensions:
        builder = builder.add_extension(extension, critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _root(common_name):
    key = ec.generate_private_key(ec.SECP256R1())
    return _issue(common_name, key, _name(common_name), key, ca=True), key


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """root -> intermediate -> leaf for 127.0.0.1, plus an unrelated root."""
    out = tmp_path_factory.mktemp("pki")

    root, root_key = _root("ci-core test root (not in certifi)")
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = _issue(
        "ci-core test intermediate", inter_key, root.subject, root_key, ca=True
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _issue(
        "127.0.0.1",
        leaf_key,
        inter.subject,
        inter_key,
        ca=False,
        extensions=[
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
        ],
    )
    other_root, _ = _root("ci-core unrelated test root")

    paths = SimpleNamespace(
        root=out / "root.pem",
        other_root=out / "other_root.pem",
        # Leaf *and* intermediate: like NTIA, the server sends a complete chain,
        # and the only thing a client can lack is the root.
        chain=out / "chain.pem",
        key=out / "leaf.key",
    )
    paths.root.write_bytes(_pem(root))
    paths.other_root.write_bytes(_pem(other_root))
    paths.chain.write_bytes(_pem(leaf) + _pem(inter))
    paths.key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()

    def log_message(self, *_args):
        pass


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A client that rejects the certificate aborts the connection, which
        # the handler sees as a reset. That is the test passing, not an error.
        pass


@pytest.fixture(scope="module")
def server_url(pki):
    server = _QuietServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(pki.chain), str(pki.key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def _trusting(root_path):
    """A context standing in for an OS trust store that holds ``root_path``."""
    return lambda: ssl.create_default_context(cafile=str(root_path))


class TestTheContextIsTheOSVerifier:
    def test_it_is_a_truststore_context(self):
        context = http._os_trust_context()
        assert isinstance(context, truststore.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_each_session_gets_its_own(self):
        """urllib3 writes verify_mode and CA files into the context it is
        handed; a shared one would carry that between the resolver's threads."""
        assert http._os_trust_context() is not http._os_trust_context()

    def test_every_https_pool_uses_it_proxied_or_not(self):
        with http._os_trust_session() as session:
            adapter = session.get_adapter("https://www.ntia.gov/")
            direct = adapter.poolmanager.connection_pool_kw["ssl_context"]
            proxied = adapter.proxy_manager_for(
                "http://proxy.invalid:3128"
            ).connection_pool_kw["ssl_context"]

        assert isinstance(direct, truststore.SSLContext)
        # Tunnelled HTTPS takes its context from the proxy manager; without the
        # override, setting HTTPS_PROXY would silently restore certifi.
        assert isinstance(proxied, truststore.SSLContext)


class TestTheCertificateErrorPath:
    def test_certifi_alone_rejects_a_root_it_does_not_ship(self, server_url):
        """The control: what happened to every NTIA citation."""
        with pytest.raises(
            requests.exceptions.SSLError,
            match="unable to get local issuer certificate",
        ):
            requests.get(server_url, timeout=5)

    def test_a_chain_the_os_trusts_is_fetched(self, pki, server_url, monkeypatch):
        monkeypatch.setattr(http, "_os_trust_context", _trusting(pki.root))

        assert http.os_trust_get(server_url, timeout=5).text == "ok"
        assert http.os_trust_head(server_url, timeout=5).status_code == 200

    def test_safe_get_takes_the_same_path(self, pki, server_url, monkeypatch):
        """safe_get is what the citation resolver and --url mode call."""
        monkeypatch.setattr(http, "_os_trust_context", _trusting(pki.root))
        # The SSRF guard refuses loopback by design; it is not under test here.
        monkeypatch.setattr(http, "_guard", lambda url: None)

        resp = http.safe_get(server_url, timeout=5)
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_a_chain_nobody_trusts_still_fails(self, pki, server_url, monkeypatch):
        """Trust is widened, never switched off: a root in neither store is
        refused exactly as before."""
        monkeypatch.setattr(http, "_os_trust_context", _trusting(pki.other_root))
        monkeypatch.setattr(http, "_guard", lambda url: None)

        with pytest.raises(requests.exceptions.SSLError):
            http.os_trust_get(server_url, timeout=5)
        with pytest.raises(requests.exceptions.SSLError):
            http.safe_get(server_url, timeout=5)
