# refunds.py — Devolución automática de USDC desde el comercio a una wallet.
"""Envía USDC (transfer_checked) desde la ATA del comercio a otra wallet.

La wallet del comercio debe tener clave privada (firmante) y SOL para la
comisión de red. Si la ATA de USDC del destino no existe, se crea en la misma
transacción (el rent lo paga el comercio).

Nunca imprime ni expone la clave privada.
"""
from __future__ import annotations

import logging

from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import create_associated_token_account, transfer_checked
from spl.token.models import TransferCheckedParams
from solders.token.associated import get_associated_token_address

logger = logging.getLogger(__name__)


async def send_usdc_refund(
    client,
    keypair: Keypair,
    mint: Pubkey,
    to_owner: Pubkey,
    amount_base: int,
    decimals: int,
) -> str:
    """Envía `amount_base` (unidades base) de USDC a `to_owner`.

    Devuelve la firma de la transacción. Lanza excepción si falla.
    """
    if amount_base <= 0:
        raise ValueError("El monto a devolver debe ser mayor que cero")

    merchant = keypair.pubkey()
    merchant_ata = get_associated_token_address(merchant, mint)
    dest_ata = get_associated_token_address(to_owner, mint)

    balance = (await client.get_token_account_balance(merchant_ata)).value
    if balance.amount is None or int(balance.amount) < amount_base:
        raise RuntimeError(
            f"Saldo USDC insuficiente en la ATA del comercio: "
            f"{balance.ui_amount} < {amount_base / (10 ** decimals):.6f}"
        )

    instructions = []
    info = await client.get_account_info_json_parsed(dest_ata)
    if info.value is None:
        logger.info("Creando ATA destino %s para %s", dest_ata, to_owner)
        instructions.append(
            create_associated_token_account(
                payer=merchant,
                owner=to_owner,
                mint=mint,
                token_program_id=TOKEN_PROGRAM_ID,
            )
        )

    instructions.append(
        transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=merchant_ata,
                mint=mint,
                dest=dest_ata,
                owner=merchant,
                amount=amount_base,
                decimals=decimals,
                signers=[],
            )
        )
    )

    blockhash = (await client.get_latest_blockhash()).value.blockhash
    msg = MessageV0.try_compile(
        payer=merchant,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    resp = await client.send_raw_transaction(bytes(tx))
    signature = str(resp.value)
    logger.info(
        "Reembolso enviado: %.6f USDC -> %s (sig %s)",
        amount_base / (10 ** decimals),
        to_owner,
        signature,
    )
    return signature
