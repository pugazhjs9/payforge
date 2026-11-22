"""Generate the ES256 signing key for access tokens.

    uv run python -m payforge.keys secrets/jwt-es256.pem

then set JWT_PRIVATE_KEY_FILE=secrets/jwt-es256.pem. The secrets/ folder is git-ignored.
"""

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main(path: str) -> None:
    target = Path(path)
    if target.exists():
        sys.exit(f"{target} already exists; not overwriting it")
    target.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    target.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    target.chmod(0o600)  # readable by its owner only
    print(f"wrote {target}; now set JWT_PRIVATE_KEY_FILE={target}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "secrets/jwt-es256.pem")
