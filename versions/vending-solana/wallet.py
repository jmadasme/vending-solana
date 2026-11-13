# wallet.py — Wallet del comercio: keypair nuevo + ATA de USDC.
"""Genera/carga el keypair del comercio y deriva/crea su ATA de USDC.

Nunca expone la clave privada fuera de este módulo: solo devuelve el pubkey
y la dirección de la ATA. La keypair se guarda como JSON (formato estándar
de Solana: lista de enteros) en la ruta indicada por config.yaml / .env,
fuera de git (ver .gitignore: wallet.json).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.constants import TOKEN_PROGRAM_ID
from solders.token.associated import get_associated_token_address

logger = logging.getLogger(__name__)

# Tamaño mínimo de cuenta de mint (82 bytes) para rent-exemption.
MINT_ACCOUNT_SPACE = 82


def load_or_create_keypair(path: Path | str) -> Keypair:
    """Carga el keypair existente o crea uno nuevo en `path` (permisos 0600)."""
    path = Path(path)
    if path.exists():
        raw = json.loads(path.read_text())
        if isinstance(raw, str):  # base58
            return Keypair.from_base58_string(raw)
        if isinstance(raw, list):
            return Keypair.from_bytes(bytes(raw))
        raise ValueError(f"Formato de keypair no reconocido en {path}")

    kp = Keypair()  # keypair aleatorio nuevo
    path.parent.mkdir(parents=True, exist_ok=True)
    # formato estándar solana CLI: lista de enteros (bytes de la keypair)
    path.write_text(json.dumps(list(bytes(kp))))
    os.chmod(path, 0o600)
    logger.info("Keypair nuevo generado en %s (pubkey=%s)", path, kp.pubkey())
    return kp


def derive_ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Deriva (sin crear) la ATA de `owner` para `mint`."""
    return get_associated_token_address(owner, mint)


async def get_or_create_ata(client, payer: Keypair, owner: Pubkey, mint: Pubkey) -> tuple[Pubkey, bool]:
    """Devuelve (ata, created). Crea la ATA en devnet si no existe.

    `payer` firma y paga el rent (necesita SOL).
    """
    from solana.rpc.async_api import AsyncClient
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from spl.token.instructions import create_associated_token_account

    ata = derive_ata(owner, mint)

    info = await client.get_account_info_json_parsed(ata)
    if info.value is not None:
        return ata, False

    logger.info("Creando ATA %s para mint %s", ata, mint)
    ix = create_associated_token_account(
        payer=payer.pubkey(),
        owner=owner,
        mint=mint,
        token_program_id=TOKEN_PROGRAM_ID,
    )
    blockhash = (await client.get_latest_blockhash()).value.blockhash
    msg = MessageV0.try_compile(
        payer=payer.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [payer])
    resp = await client.send_raw_transaction(bytes(tx))
    return ata, True