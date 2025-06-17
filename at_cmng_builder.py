"""at_cmng_builder.py — light‑weight helper for nRF91 %CMNG writes
================================================================

*No more auto‑generated CAs.*  This module now focuses on one job:
turn **existing** PEM files into the exact multiline `%CMNG` write commands
required by the modem — plus SHA‑256 digests so you can verify with
`%CMNG=1` afterwards.

Public API
----------
```python
make_cmng_write(sec_tag, type, pem)      # one command string
build_cmng_commands(sec_tag, root_ca, client_crt, client_key) -> list[str]
issue_with_ca(sec_tag, client_cn, ca_crt_path, ca_key_path) -> (cmds, pem_dict)
    # signs a client cert/key with your CA and returns:
    #   cmds      – list[str] three %CMNG commands (types 0,1,2)
    #   pem_dict  – {root_ca, client_crt, client_key, sha}

pem_sha(pem)                             # 64‑char SHA‑256 like the modem shows
build_sha_map(root, crt, key)            # {0:…,1:…,2:…}
```

The module is **library‑only** – no CLI or serial I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple
import datetime

try:
    # Only needed for issue_with_ca()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:  # cryptography optional unless issue_with_ca() is used
    x509 = None  # type: ignore

__all__ = [
    "make_cmng_write",
    "build_cmng_commands",
    "pem_sha",
    "build_sha_map",
    "issue_with_ca",
]

# ───────────────────────── basic helpers ─────────────────────────

def _normalize_pem(pem: str) -> str:
    """Normalize line endings and strip final blank line."""
    return pem.replace("\r\n", "\n").rstrip("\n")


def make_cmng_write(sec_tag: int, cmng_type: int, pem: str, *, opcode: int = 0) -> str:
    """Return one `%CMNG=<opcode>,<tag>,<type>,"<PEM>"` string.

    A leading newline after the opening quote mirrors Nordic’s examples and
    ensures the modem stores the PEM exactly as on disk.
    """
    if not pem.lstrip().startswith("-----BEGIN"):
        raise ValueError("PEM must start with '-----BEGIN')")
    clean = _normalize_pem(pem)
    return f'AT%CMNG={opcode},{sec_tag},{cmng_type},"\n{clean}"'


def build_cmng_commands(
    sec_tag: int,
    root_ca_pem: str,
    client_cert_pem: str,
    client_key_pem: str,
    *,
    opcode: int = 0,
) -> List[str]:
    """Return three write commands — types 0 (root), 1 (cert), 2 (key)."""
    return [
        make_cmng_write(sec_tag, 0, root_ca_pem, opcode=opcode),
        make_cmng_write(sec_tag, 1, client_cert_pem, opcode=opcode),
        make_cmng_write(sec_tag, 2, client_key_pem, opcode=opcode),
    ]

# ───────────────────────── SHA helper ───────────────────────────

def _sha256_hex(data: str | bytes) -> str:
    import hashlib, binascii

    if isinstance(data, str):
        data = data.encode()
    return binascii.hexlify(hashlib.sha256(data).digest()).upper().decode()


def pem_sha(pem: str) -> str:
    """Return the SHA-256 digest exactly as the modem stores it."""
    payload = "\n" + _normalize_pem(pem)   # leading LF + normalized PEM
    return _sha256_hex(payload)   


def build_sha_map(root_ca_pem: str, client_crt_pem: str, client_key_pem: str) -> dict[int, str]:
    """Digest map matching `%CMNG=1` list order."""
    return {
        0: pem_sha(root_ca_pem),
        1: pem_sha(client_crt_pem),
        2: pem_sha(client_key_pem),
    }

# ───────────────────────── optional: sign with existing CA ──────

def _require_crypto():
    if x509 is None:
        raise RuntimeError("`pip install cryptography` required for issue_with_ca()")


def _gen_client_cert(cn: str, ca_key_pem: bytes, ca_cert_pem: bytes, days: int = 365) -> Tuple[bytes, bytes]:
    ca_key = _ser.load_pem_private_key(ca_key_pem, password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)

    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .sign(key, hashes.SHA256())
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=days))
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        _ser.Encoding.PEM,
        _ser.PrivateFormat.TraditionalOpenSSL,
        _ser.NoEncryption(),
    )
    cert_pem = cert.public_bytes(_ser.Encoding.PEM)
    return key_pem, cert_pem


def issue_with_ca(
    sec_tag: int,
    client_cn: str,
    *,
    ca_crt_path: str | Path,
    ca_key_path: str | Path,
    days: int = 365,
) -> Tuple[List[str], dict]:
    """Use an existing CA key/cert to sign a client certificate & build commands.

    Returns `(cmds, pem_dict)` where `pem_dict` contains:
        root_ca, client_crt, client_key, sha
    """
    _require_crypto()
    ca_crt = Path(ca_crt_path).read_bytes()
    ca_key = Path(ca_key_path).read_bytes()

    cli_key, cli_crt = _gen_client_cert(client_cn, ca_key, ca_crt, days)
    cmds = build_cmng_commands(sec_tag, ca_crt.decode(), cli_crt.decode(), cli_key.decode())
    sha = build_sha_map(ca_crt.decode(), cli_crt.decode(), cli_key.decode())

    return cmds, {
        "root_ca": ca_crt.decode(),
        "client_crt": cli_crt.decode(),
        "client_key": cli_key.decode(),
        "sha": sha,
    }

# Example usage:
if __name__ == "__main__":
    cmds, pems = issue_with_ca(
        sec_tag      = 16842753,
        client_cn    = "msense",
        ca_crt_path  = "certs/ca.crt",
        ca_key_path  = "certs/ca.key",
        days         = 3650,        # optional
    )

    for c in cmds:
        print(c)   # three %CMNG write commands

    print("SHA map:", pems["sha"])  # SHA-256 digests for verification