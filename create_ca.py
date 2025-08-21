#!/usr/bin/env python3
"""

"""

from pathlib import Path
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import os
from dotenv import load_dotenv

# --- Settings---


#load from env if env file:

load_dotenv()

DOMAIN = os.getenv("DOMAIN","subdomain.domain.com")
DAYS = int(os.getenv("DAYS", "3650"))
CURVE = os.getenv("CURVE", "prime256v1").lower()

CA_CN = f"root.{DOMAIN}"
SERVER_CN = f"{DOMAIN}"
CLIENT_CN = f"client.{DOMAIN}"

# --- Paths (match your bash layout) ---
BASE = Path("certs")
CA_DIR = BASE
SERVER_DIR = BASE / "server"
CLIENT_DIR = BASE / "client"

CA_KEY_PATH = CA_DIR / "ca.key"
CA_CRT_PATH = CA_DIR / "ca.crt"

SERVER_KEY_PATH = SERVER_DIR / "server.key"
SERVER_CSR_PATH = SERVER_DIR / "server.csr"
SERVER_CRT_PATH = SERVER_DIR / "server.crt"


def ensure_dirs():
    # If dirs are not empty warn user
    for d in (CA_DIR, SERVER_DIR, CLIENT_DIR):
        try:
            if any(d.iterdir()):
                print(f"Warning: {d} is not empty do you want to proceed? (y/n)")
                if input().strip().lower() != "y":
                    print("Aborting...")
                    exit(1)
        except Exception as e:
            print(f"Dir {d} does not exist")
    
    CA_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)  # to match your script


def ec_curve_from_name(name: str):
    name = name.lower()
    if name in ("prime256v1", "secp256r1"):
        return ec.SECP256R1()
    # Add more mappings if you ever change CURVE
    raise ValueError(f"Unsupported curve: {name}")


def save_private_key_traditional_openssl(key, path: Path):
    # Openssl ecparam outputs "TraditionalOpenSSL" (SEC1) format for EC keys by default.
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)


def save_pem(path: Path, data: bytes):
    path.write_bytes(data)


def build_name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def main():
    ensure_dirs()

    # --- CA: key + self-signed cert ---
    print("Generating CA certificate...")
    ca_key = ec.generate_private_key(ec_curve_from_name(CURVE))
    save_private_key_traditional_openssl(ca_key, CA_KEY_PATH)

    ca_subject = build_name(CA_CN)
    now = datetime.now(timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)  # self-signed
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=DAYS))
        # Add minimal but sensible extensions. (openssl x509 adds very little by default.)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    save_pem(CA_CRT_PATH, ca_cert.public_bytes(serialization.Encoding.PEM))

    # --- Server: key + CSR ---
    print("Generating Server certificate...")
    server_key = ec.generate_private_key(ec_curve_from_name(CURVE))
    save_private_key_traditional_openssl(server_key, SERVER_KEY_PATH)

    server_subject = build_name(SERVER_CN)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(server_subject)
        # Note: The bash command didn’t add SANs; we keep parity and omit them here.
        .sign(server_key, hashes.SHA256())
    )
    save_pem(SERVER_CSR_PATH, csr.public_bytes(serialization.Encoding.PEM))

    # --- Sign server CSR with CA ---
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())  # openssl -CAcreateserial equivalent
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # KeyUsage here is optional; openssl default is very permissive. Kept minimal.
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )
    save_pem(SERVER_CRT_PATH, server_cert.public_bytes(serialization.Encoding.PEM))

    print("Done.")
    print(f"CA key : {CA_KEY_PATH}")
    print(f"CA cert: {CA_CRT_PATH}")
    print(f"Server key: {SERVER_KEY_PATH}")
    print(f"Server CSR: {SERVER_CSR_PATH}")
    print(f"Server cert: {SERVER_CRT_PATH}")


if __name__ == "__main__":
    main()