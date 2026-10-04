"""What a connection to Qdrant is verified against.

`SSL_CERT_FILE` names the stack's own CA. A bare `verify=<path>` trusts that file alone,
which would make a Qdrant outside the stack, signed by a public CA, unreachable the
moment a connection to one exists. `tls_verify` therefore trusts both.
"""
from __future__ import annotations

import datetime
import ssl
from pathlib import Path

import certifi
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core.qdrant import tls_verify


def _local_ca(path: Path) -> Path:
    """A self-signed CA certificate, standing in for the stack's own."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "papaia-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path


def _public_cas() -> int:
    context = ssl.create_default_context(cafile=certifi.where())
    return int(context.cert_store_stats()["x509_ca"])


def test_without_a_bundle_the_public_cas_are_used() -> None:
    assert tls_verify(None) is True
    assert tls_verify("") is True


def test_with_a_bundle_the_stacks_ca_is_added_to_the_public_ones(tmp_path: Path) -> None:
    context = tls_verify(str(_local_ca(tmp_path / "local-ca.crt")))

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.cert_store_stats()["x509_ca"] == _public_cas() + 1


def test_a_bundle_that_cannot_be_read_costs_the_local_ca_and_nothing_else(
    tmp_path: Path,
) -> None:
    context = tls_verify(str(tmp_path / "missing.crt"))

    assert isinstance(context, ssl.SSLContext)
    assert context.cert_store_stats()["x509_ca"] == _public_cas()


def test_the_context_is_built_once_per_bundle(tmp_path: Path) -> None:
    bundle = str(_local_ca(tmp_path / "local-ca.crt"))

    assert tls_verify(bundle) is tls_verify(bundle)


@pytest.mark.parametrize("value", [None, ""])
def test_no_bundle_is_the_same_whatever_it_is_called(value: str | None) -> None:
    assert tls_verify(value) is True
