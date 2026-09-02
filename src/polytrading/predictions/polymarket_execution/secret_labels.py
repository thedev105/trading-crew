"""Canonical fixed labels for the Polymarket pilot secret boundary."""

from __future__ import annotations

from typing import Final

CLOB_SERVICE: Final = "polytrading.polymarket.pilot"
WALLET_PRIVATE_KEY_ACCOUNT: Final = "wallet-private-key"
CLOB_API_KEY_ACCOUNT: Final = "clob-api-key"
CLOB_API_SECRET_ACCOUNT: Final = "clob-api-secret"
CLOB_PASSPHRASE_ACCOUNT: Final = "clob-passphrase"
ALLOWED_ACCOUNTS: Final = frozenset(
    {
        WALLET_PRIVATE_KEY_ACCOUNT,
        CLOB_API_KEY_ACCOUNT,
        CLOB_API_SECRET_ACCOUNT,
        CLOB_PASSPHRASE_ACCOUNT,
    }
)

__all__ = [
    "ALLOWED_ACCOUNTS",
    "CLOB_API_KEY_ACCOUNT",
    "CLOB_API_SECRET_ACCOUNT",
    "CLOB_PASSPHRASE_ACCOUNT",
    "CLOB_SERVICE",
    "WALLET_PRIVATE_KEY_ACCOUNT",
]
