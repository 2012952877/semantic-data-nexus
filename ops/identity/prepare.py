"""Generate disposable synthetic IdP identities and private local TLS material, never live secrets."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent / ".generated"


def prepare() -> None:
    ROOT.mkdir(exist_ok=False)
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic local identity CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=7))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "identity.localhost")]))
            .issuer_name(name).public_key(tls_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=7))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("identity.localhost"), x509.DNSName("localhost"),
                x509.DNSName("control-api"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
            ]), critical=False).sign(ca_key, hashes.SHA256()))
    (ROOT / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (ROOT / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (ROOT / "tls.key").write_bytes(tls_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (ROOT / "tls.pfx").write_bytes(pkcs12.serialize_key_and_certificates(
        b"nexus-local", tls_key, cert, [ca], serialization.NoEncryption()))
    service_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signing_material = service_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    public_material = service_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    synthetic_credentials = [secrets.token_urlsafe(32) for _ in range(3)]
    (ROOT / "compose.env").write_text(f"IDENTITY_DATABASE_PASSWORD={synthetic_credentials[0]}\n")
    (ROOT / "control.env").write_text("\n".join([
        f"RunStorage__ConnectionString=Host=postgres;Database=nexus_identity;Username=nexus_identity;Password={synthetic_credentials[0]}",
        "Identity__Providers__0__Name=Local",
        "Identity__Providers__0__Authority=https://identity.localhost:8443/realms/nexus",
        "Identity__Providers__0__ClientId=nexus-browser",
        f"Identity__Providers__0__ClientSecret={synthetic_credentials[1]}",
        "Identity__Providers__0__Audience=nexus-api",
        f"Identity__ServiceSigningKey={json.dumps(signing_material)}",
    ]) + "\n")
    (ROOT / "backend.env").write_text("\n".join([
        f"SEMANTIC_NEXUS_SERVICE_PUBLIC_KEY={json.dumps(public_material)}",
        f"SEMANTIC_NEXUS_IDENTITY_POSTGRES=host=postgres dbname=nexus_identity user=nexus_identity password={synthetic_credentials[0]}",
    ]) + "\n")
    users = [
        {"id": "synthetic-alice", "username": "alice", "enabled": True, "emailVerified": True,
         "firstName": "Synthetic", "lastName": "Alice",
         "credentials": [{"type": "password", "value": synthetic_credentials[2], "temporary": False}]},
        {"id": "synthetic-bob", "username": "bob", "enabled": True, "emailVerified": True,
         "firstName": "Synthetic", "lastName": "Bob",
         "credentials": [{"type": "password", "value": synthetic_credentials[2], "temporary": False}]},
    ]
    synthetic_access_lifetime = 300
    realm = {
        "realm": "nexus", "enabled": True, "sslRequired": "all",
        "registrationAllowed": False, "resetPasswordAllowed": False,
        "accessTokenLifespan": synthetic_access_lifetime, "ssoSessionIdleTimeout": 1800,
        "users": users, "clients": [{
            "clientId": "nexus-browser", "secret": synthetic_credentials[1],
            "publicClient": False, "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
            "redirectUris": ["https://localhost:8444/auth/callback/Local"],
            "webOrigins": ["https://localhost:8444"],
            "attributes": {"pkce.code.challenge.method": "S256"},
        }]
    }
    (ROOT / "realm.json").write_text(json.dumps(realm))
    (ROOT / "browser.json").write_text(json.dumps({"password": synthetic_credentials[2]}))
    for workspace, tenant in [("workspace-a", "tenant-a"), ("workspace-b", "tenant-b")]:
        (ROOT / f"bootstrap-{workspace}.json").write_text(json.dumps({
            "Operation": "bootstrap", "RequestId": f"bootstrap-{workspace}",
            "WorkspaceId": workspace, "TenantId": tenant, "Name": f"Synthetic {workspace}",
            "PrincipalId": "principal-alice", "MembershipId": f"membership-alice-{workspace}",
            "Provider": "Local", "Subject": "synthetic-alice",
        }))
    (ROOT / "keyring").mkdir()
    # Disposable local-only mounts are shared with non-root container UIDs.
    os.chmod(ROOT / "keyring", 0o777)
    for path in ROOT.iterdir():
        if path.is_file():
            os.chmod(path, 0o644)
    print("Generated disposable local identity fixtures; no values printed.")


if __name__ == "__main__":
    prepare()
