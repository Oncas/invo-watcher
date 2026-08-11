from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

import base58
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

# Turnkey HPKE constants (from @turnkey/crypto)
HPKE_VERSION = bytes([72, 80, 75, 69, 45, 118, 49])  # HPKE-v1
SUITE_ID_1 = bytes([75, 69, 77, 0, 16])
SUITE_ID_2 = bytes([72, 80, 75, 69, 0, 16, 0, 1, 0, 2])
LABEL_SECRET = b"secret"
LABEL_EAE_PRK = b"eae_prk"
LABEL_SHARED_SECRET = b"shared_secret"
AES_KEY_INFO = bytes(
    [
        0, 32, 72, 80, 75, 69, 45, 118, 49, 72, 80, 75, 69, 0, 16, 0, 1, 0, 2, 107,
        101, 121, 0, 143, 195, 174, 184, 50, 73, 10, 75, 90, 179, 228, 32, 35, 40,
        125, 178, 154, 31, 75, 199, 194, 34, 192, 223, 34, 135, 39, 183, 10, 64, 33,
        18, 47, 63, 4, 233, 32, 108, 209, 36, 19, 80, 53, 41, 180, 122, 198, 166, 48,
        185, 46, 196, 207, 125, 35, 69, 8, 208, 175, 151, 113, 201, 158, 80,
    ]
)
IV_INFO = bytes(
    [
        0, 12, 72, 80, 75, 69, 45, 118, 49, 72, 80, 75, 69, 0, 16, 0, 1, 0, 2, 98, 97,
        115, 101, 95, 110, 111, 110, 99, 101, 0, 143, 195, 174, 184, 50, 73, 10, 75,
        90, 179, 228, 32, 35, 40, 125, 178, 154, 31, 75, 199, 194, 34, 192, 223, 34,
        135, 39, 183, 10, 64, 33, 18, 47, 63, 4, 233, 32, 108, 209, 36, 19, 80, 53,
        41, 180, 122, 198, 166, 48, 185, 46, 196, 207, 125, 35, 69, 8, 208, 175, 151,
        113, 201, 158, 80,
    ]
)


@dataclass
class P256KeyPair:
    private_key_hex: str
    public_key_compressed_hex: str
    # Involio /dex/auth/otp/login expects uncompressed SEC1 with 0x prefix
    # (matches Flutter: Fo(aGN(ecr(compressed))) → "0x04…").
    public_key_otp_hex: str


def generate_p256_keypair() -> P256KeyPair:
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_comp = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )
    pub_uncomp = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return P256KeyPair(
        private_key_hex=priv_bytes.hex(),
        public_key_compressed_hex=pub_comp.hex(),
        public_key_otp_hex="0x" + pub_uncomp.hex(),
    )


def public_key_from_private(priv_hex: str) -> str:
    priv = ec.derive_private_key(int(priv_hex, 16), ec.SECP256R1(), default_backend())
    return priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    ).hex()


def _priv_to_uncompressed(priv_hex: str) -> bytes:
    priv = ec.derive_private_key(int(priv_hex, 16), ec.SECP256R1(), default_backend())
    return priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def _uncompress_p256(compressed: bytes) -> bytes:
    if len(compressed) != 33 or compressed[0] not in (2, 3):
        raise ValueError("invalid compressed P-256 public key")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), compressed)
    return pub.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def _build_labeled_ikm(label: bytes, ikm: bytes, suite_id: bytes) -> bytes:
    return HPKE_VERSION + suite_id + label + ikm


def _build_labeled_info(label: bytes, info: bytes, suite_id: bytes, length: int) -> bytes:
    out = bytearray(9 + len(suite_id) + len(label) + len(info))
    out[0] = 0
    out[1] = length
    out[2:9] = HPKE_VERSION
    start = 9
    out[start : start + len(suite_id)] = suite_id
    start += len(suite_id)
    out[start : start + len(label)] = label
    start += len(label)
    out[start : start + len(info)] = info
    return bytes(out)


def _extract_and_expand(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    # Match @noble/hashes hkdf.extract(sha256, ikm, salt)
    if len(salt) == 0:
        salt = bytes(hashlib.sha256().digest_size)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return HKDFExpand(
        algorithm=hashes.SHA256(),
        length=length,
        info=info,
        backend=default_backend(),
    ).derive(prk)


def _ecdh_shared_secret(priv_hex: str, peer_uncompressed: bytes) -> bytes:
    priv = ec.derive_private_key(int(priv_hex, 16), ec.SECP256R1(), default_backend())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_uncompressed)
    return priv.exchange(ec.ECDH(), peer)


def hpke_decrypt(
    *,
    ciphertext: bytes,
    encapped_key_uncompressed: bytes,
    receiver_priv_hex: str,
) -> bytes:
    receiver_pub = _priv_to_uncompressed(receiver_priv_hex)
    aad = encapped_key_uncompressed + receiver_pub
    ss = _ecdh_shared_secret(receiver_priv_hex, encapped_key_uncompressed)
    kem_context = encapped_key_uncompressed + receiver_pub

    ikm = _build_labeled_ikm(LABEL_EAE_PRK, ss, SUITE_ID_1)
    info = _build_labeled_info(LABEL_SHARED_SECRET, kem_context, SUITE_ID_1, 32)
    shared_secret = _extract_and_expand(b"", ikm, info, 32)

    ikm = _build_labeled_ikm(LABEL_SECRET, b"", SUITE_ID_2)
    key = _extract_and_expand(shared_secret, ikm, AES_KEY_INFO, 32)
    iv = _extract_and_expand(shared_secret, ikm, IV_INFO, 12)
    return AESGCM(key).decrypt(iv, ciphertext, aad)


def decrypt_credential_bundle(credential_bundle: str, embedded_private_key_hex: str) -> str:
    """Return API private key hex decrypted from a Turnkey credential bundle."""
    bundle_bytes = base58.b58decode_check(credential_bundle)
    if len(bundle_bytes) <= 33:
        raise ValueError("credential bundle too short")
    compressed_encapped = bundle_bytes[:33]
    ciphertext = bundle_bytes[33:]
    encapped = _uncompress_p256(compressed_encapped)
    decrypted = hpke_decrypt(
        ciphertext=ciphertext,
        encapped_key_uncompressed=encapped,
        receiver_priv_hex=embedded_private_key_hex,
    )
    return decrypted.hex()


def new_client_tx_id() -> str:
    return str(uuid.uuid4())
