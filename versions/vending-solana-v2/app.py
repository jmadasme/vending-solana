# app.py — Web app FastAPI de la máquina expendedora cripto v2.
"""Punto de entrada (uvicorn app:app). Endpoints:

- GET  /                      -> grilla de 9 bebidas + carrito
- POST /order                 -> crea orden multi-item + Transfer Request + QR
- GET  /order/{order_id}      -> estado de la orden (JSON, polling)
- GET  /order/{order_id}/qr   -> PNG del QR
- GET  /order/{order_id}/payment   -> HTML página de pago
- GET  /order/{order_id}/confirmed -> HTML "pago confirmado / celebración"
- GET  /order/{order_id}/expired   -> HTML "orden expirada"
- POST /order/{order_id}/cancel   -> cancela si pendiente
- GET  /order/{order_id}/cancelled -> HTML "orden cancelada"
- GET  /healthz               -> salud (RPC + GPIO + wallet)
- POST /api/test/dispense     -> prueba manual del dispensador (TEST_TOKEN)

v2: carrito de compra con máximo 1 unidad por producto; la orden se crea con el
total del carrito; al confirmar el pago se dispensa SECUENCIALMENTE un pulso de
2.5 s por cada bebida del carrito (todas por GPIO 17).
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

import qrcode
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from solders.keypair import Keypair
from solders.pubkey import Pubkey

import payments
import refunds
from config import load_config
from dispenser import Dispenser
from wallet import derive_ata, get_or_create_ata, load_merchant_wallet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vending")

cfg = load_config()
templates = Jinja2Templates(directory=str(cfg.base_dir / "templates"))

# ----------------------------------------------------------------------
# Catálogo
# ----------------------------------------------------------------------
import yaml  # noqa: E402


def load_catalog() -> list[dict]:
    data = yaml.safe_load((cfg.base_dir / "catalog.yaml").read_text())
    return data["products"]


PRODUCTS = load_catalog()
PRODUCTS_BY_SKU = {p["sku"]: p for p in PRODUCTS}


# ----------------------------------------------------------------------
# Modelos / estado
# ----------------------------------------------------------------------
class CreateOrderItem(BaseModel):
    sku: str


class CreateOrderRequest(BaseModel):
    items: list[CreateOrderItem]


@dataclass
class OrderItem:
    sku: str
    product_name: str
    price_usdc: float
    amount_base: int
    dispensed: bool = False


@dataclass
class Order:
    order_id: str
    items: list[OrderItem]
    total_amount_base: int
    total_usdc: float
    reference: str
    status: str = "pendiente"  # pendiente|pagada|dispensada|expirada|monto_incorrecto
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    signature: Optional[str] = None
    paid_amount: Optional[int] = None
    refund_status: str = "none"  # none|scheduled|sending|sent|failed|disabled
    refund_due_at: Optional[float] = None
    refund_amount_base: Optional[int] = None
    refund_signature: Optional[str] = None
    refund_error: Optional[str] = None

    @property
    def remaining_s(self) -> int:
        return max(0, int(self.expires_at - time.time()))


ORDERS: dict[str, Order] = {}
ORDERS_LOCK = asyncio.Lock()
ORDERS_BY_REFERENCE: dict[str, str] = {}
REFUND_TASKS: set[asyncio.Task] = set()

# Pagos recibidos sin una orden pendiente asociada (para devolución manual).
UNMATCHED_PAYMENTS: list[dict] = []


# ----------------------------------------------------------------------
# Dependencias de la app (wallet, ATA, dispensador, monitor)
# ----------------------------------------------------------------------
keypair: Optional[Keypair] = None
merchant_pubkey: Optional[Pubkey] = None
merchant_ata: Optional[Pubkey] = None
dispenser: Optional[Dispenser] = None
payment_monitor: Optional[payments.PaymentMonitor] = None
rpc_client = None


def _resolve_rpc_http() -> str:
    return cfg.rpc_http


def _rpc_http_backup() -> Optional[str]:
    """Devuelve el RPC de respaldo, o None si es igual al primario."""
    backup = cfg.rpc_http_backup
    if backup and backup != cfg.rpc_http:
        return backup
    return None


async def _init_backend() -> None:
    """Crea ATA y lanza el monitor. Se ejecuta en lifespan startup."""
    global keypair, merchant_pubkey, merchant_ata, payment_monitor, rpc_client
    from solana.rpc.async_api import AsyncClient

    rpc_client = AsyncClient(_resolve_rpc_http())
    merchant_pubkey, keypair = load_merchant_wallet(cfg.wallet_abs_path)
    mint = Pubkey.from_string(cfg.usdc_mint)
    # Derivación siempre disponible; la creación on-chain necesita SOL (rent).
    merchant_ata = derive_ata(merchant_pubkey, mint)
    if keypair is not None:
        try:
            created_ata, _ = await get_or_create_ata(rpc_client, keypair, merchant_pubkey, mint)
            merchant_ata = created_ata
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo crear la ATA on-chain (%s). Usando ATA derivada.", exc)
    else:
        logger.info(
            "Wallet watch-only (sin clave privada): no se firma nada; "
            "se cobra en la ATA ya existente."
        )
    logger.info("Wallet comercio: %s", merchant_pubkey)
    logger.info("ATA USDC comercio: %s (mint %s)", merchant_ata, mint)

    payment_monitor = payments.PaymentMonitor(
        rpc_http=_resolve_rpc_http(),
        rpc_ws=cfg.rpc_ws,
        merchant_ata=merchant_ata,
        on_payment=_on_payment,
        rpc_http_backup=_rpc_http_backup(),
        # Detección casi instantánea: el fallback de reconciliación corre cada
        # 2s (no 30s) para que el pago se detecte en ~2s incluso si el WebSocket
        # del RPC público no entrega notificaciones a tiempo.
        reconcile_interval=2.0,
    )
    await payment_monitor.start()


# Tolerancia de monto en unidades base (6 decimales): ±0.01 USDC.
# Necesaria porque wallets como Phantom pueden ajustar el amount ligeramente
# (p.ej. 2050192 en vez de 2050000) o no incluir el `reference` al digitar
# el monto manualmente.
AMOUNT_TOLERANCE = 10_000  # 0.01 USDC

# Pausa breve entre pulsos secuenciales del dispensador (segundos).
DISPENSE_BETWEEN_SECONDS = 1.0


async def _dispatch_dispense(order_id: str) -> None:
    """Dispensado SECUENCIAL: un pulso (2.5 s) por cada bebida del carrito."""
    order = ORDERS.get(order_id)
    if order is None:
        logger.error("Orden %s no encontrada al dispensar", order_id)
        return
    dispenser.set_led("paid", True)
    try:
        for item in order.items:
            logger.info("Dispensando %s (%s)…", item.sku, item.product_name)
            await dispenser.dispense(cfg.dispense_pin, cfg.dispense_pulse_seconds)
            item.dispensed = True
            await asyncio.sleep(DISPENSE_BETWEEN_SECONDS)
        order.status = "dispensada"
        logger.info("Orden %s DISPENSADA (%d bebidas)", order_id, len(order.items))
    except Exception as exc:  # noqa: BLE001
        logger.error("Fallo al dispensar orden %s: %s", order_id, exc)
        order.status = "pagada"  # pagado pero sin dispensar (reintentable)
        dispenser.set_led("error", True)
    finally:
        dispenser.set_led("paid", False)


async def _schedule_refund(order_id: str) -> None:
    """Devuelve el monto cobrado a cfg.refund_to tras cfg.refund_delay_seconds.

    Requiere una wallet firmante (keypair) y AUTO_REFUND_ENABLED=true.
    Idempotente por orden: solo actúa si refund_status == "scheduled".
    """
    order = ORDERS.get(order_id)
    if order is None:
        return

    if not cfg.auto_refund_enabled:
        order.refund_status = "disabled"
        return
    if not cfg.refund_to:
        order.refund_status = "failed"
        order.refund_error = "REFUND_TO no configurado"
        logger.warning("Reembolso de %s omitido: REFUND_TO no configurado", order_id)
        return
    if keypair is None:
        order.refund_status = "failed"
        order.refund_error = "wallet sin clave privada (watch-only)"
        logger.warning(
            "Reembolso de %s omitido: la wallet no tiene clave privada; "
            "no se puede firmar la devolución",
            order_id,
        )
        return

    amount_base = order.paid_amount if order.paid_amount is not None else order.total_amount_base
    order.refund_amount_base = amount_base
    order.refund_status = "scheduled"
    order.refund_due_at = time.time() + cfg.refund_delay_seconds
    logger.info(
        "Reembolso programado: orden %s, %.6f USDC -> %s en %ds",
        order_id,
        amount_base / (10 ** cfg.usdc_decimals),
        cfg.refund_to,
        cfg.refund_delay_seconds,
    )

    await asyncio.sleep(cfg.refund_delay_seconds)

    if order.refund_status != "scheduled":
        return
    order.refund_status = "sending"
    try:
        to_owner = Pubkey.from_string(cfg.refund_to)
        mint = Pubkey.from_string(cfg.usdc_mint)
        signature = await refunds.send_usdc_refund(
            rpc_client, keypair, mint, to_owner, amount_base, cfg.usdc_decimals
        )
        order.refund_signature = signature
        order.refund_status = "sent"
        logger.info(
            "Reembolso COMPLETADO: orden %s, %.6f USDC (sig %s)",
            order_id,
            amount_base / (10 ** cfg.usdc_decimals),
            signature,
        )
    except Exception as exc:  # noqa: BLE001
        order.refund_status = "failed"
        order.refund_error = str(exc)[:300]
        logger.error("Falló el reembolso de la orden %s: %s", order_id, exc)


async def _on_payment(payment: payments.Payment) -> None:
    """Callback del monitor: valida contra las órdenes pendientes y dispensa."""
    logger.info(
        "on_payment: amount=%d sig=%s keys=%s",
        payment.amount_base,
        payment.signature,
        (payment.account_keys or [])[:6],
    )
    order_id: Optional[str] = None
    async with ORDERS_LOCK:
        # 1) Buscar orden pendiente cuyo reference esté en la transacción
        order: Optional[Order] = None
        for ref, oid in list(ORDERS_BY_REFERENCE.items()):
            o = ORDERS.get(oid)
            if o is None or o.status != "pendiente":
                continue
            if payment.account_keys and ref in payment.account_keys:
                order = o
                break

        # 2) Fallback: emparejar por monto dentro de tolerancia (sin reference).
        #    Necesario porque Phantom NO siempre incluye el reference del QR
        #    cuando el cliente digita el monto manualmente. Toma la orden
        #    pendiente más antigua cuyo monto esperado coincida con el pago.
        if order is None:
            # Un pago viejo (p.ej. re-escaneado tras un reinicio) no debe
            # emparejarse con una orden nueva. Margen de 10 s por desfase
            # de reloj/block_time.
            pending_by_amount = [
                o for o in ORDERS.values()
                if o.status == "pendiente"
                and abs(o.total_amount_base - payment.amount_base) <= AMOUNT_TOLERANCE
                and (payment.tx_time is None or payment.tx_time >= o.created_at - 10)
            ]
            if pending_by_amount:
                pending_by_amount.sort(key=lambda o: o.created_at)
                order = pending_by_amount[0]
                logger.info(
                    "Emparejado por monto (sin reference): orden %s esperaba %d, "
                    "recibido %d (tolerancia %d)",
                    order.order_id, order.total_amount_base, payment.amount_base,
                    AMOUNT_TOLERANCE,
                )

        if order is None:
            logger.warning("Pago sin orden pendiente asociada (monto=%d)", payment.amount_base)
            # Registrar para devolución manual.
            UNMATCHED_PAYMENTS.append({
                "signature": payment.signature,
                "amount_base": payment.amount_base,
                "tx_time": payment.tx_time,
                "recorded_at": time.time(),
            })
            return

        # 3) Validar monto dentro de tolerancia
        if abs(payment.amount_base - order.total_amount_base) > AMOUNT_TOLERANCE:
            order.status = "monto_incorrecto"
            order.paid_amount = payment.amount_base
            logger.warning(
                "Monto incorrecto para orden %s: esperado %d, recibido %d",
                order.order_id, order.total_amount_base, payment.amount_base,
            )
            return

        order.status = "pagada"
        order.signature = payment.signature
        order.paid_amount = payment.amount_base
        order_id = order.order_id

    # 3) Programar devolución automática (independiente del dispensado).
    task = asyncio.create_task(_schedule_refund(order_id))
    REFUND_TASKS.add(task)
    task.add_done_callback(REFUND_TASKS.discard)

    # 4) Dispensar (fuera del lock) — secuencial por cada bebida.
    logger.info("Orden %s PAGADA -> dispensando…", order_id)
    await _dispatch_dispense(order_id)


async def _expiry_sweeper() -> None:
    """Marca como expiradas las órdenes sin pagar tras el timeout."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        async with ORDERS_LOCK:
            for order in list(ORDERS.values()):
                if order.status == "pendiente" and now > order.expires_at:
                    order.status = "expirada"
                    # Liberar el reference al expirar la orden.
                    ORDERS_BY_REFERENCE.pop(order.reference, None)
                    logger.info("Orden %s expirada", order.order_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dispenser
    dispenser = Dispenser(cfg)
    try:
        await _init_backend()
    except Exception as exc:  # noqa: BLE001
        logger.error("Falló la inicialización del backend: %s", exc)
    sweep = asyncio.create_task(_expiry_sweeper())
    yield
    sweep.cancel()
    if payment_monitor:
        await payment_monitor.stop()
    if rpc_client:
        await rpc_client.close()
    if dispenser:
        dispenser.cleanup()


app = FastAPI(title="Vending Coca-Cola v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(cfg.base_dir / "static")), name="static")


# ----------------------------------------------------------------------
# Middleware de seguridad
# ----------------------------------------------------------------------
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
        " script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
    )
    return response


# Throttle simple por IP para POST /order.
_ORDER_THROTTLE: dict[str, list[float]] = {}
_ORDER_THROTTLE_WINDOW = 5.0  # segundos
_ORDER_THROTTLE_MAX = 5  # máximo de órdenes por ventana por IP


def _throttle_order(client_ip: str) -> None:
    now = time.time()
    bucket = _ORDER_THROTTLE.setdefault(client_ip, [])
    # Purgar timestamps fuera de la ventana.
    _ORDER_THROTTLE[client_ip] = [t for t in bucket if now - t < _ORDER_THROTTLE_WINDOW]
    if len(_ORDER_THROTTLE[client_ip]) >= _ORDER_THROTTLE_MAX:
        raise HTTPException(429, "Demasiadas órdenes creadas. Intenta en unos segundos.")
    _ORDER_THROTTLE[client_ip].append(now)


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"products": PRODUCTS}
    )


@app.post("/order")
async def create_order(req: CreateOrderRequest, request: Request):
    # Throttle simple por IP.
    client_ip = request.client.host if request.client else "unknown"
    _throttle_order(client_ip)

    if merchant_pubkey is None or merchant_ata is None:
        raise HTTPException(503, "Backend no inicializado")

    # Validar skus contra catálogo y recalcular el total (nunca confiar en el
    # total enviado por el cliente).
    if not req.items:
        raise HTTPException(400, "El carrito está vacío")

    seen: set[str] = set()
    items: list[OrderItem] = []
    for it in req.items:
        sku = it.sku
        product = PRODUCTS_BY_SKU.get(sku)
        if product is None:
            raise HTTPException(404, f"Producto desconocido: {sku}")
        if sku in seen:
            raise HTTPException(400, f"Producto duplicado en el carrito: {sku}")
        seen.add(sku)
        items.append(OrderItem(
            sku=product["sku"],
            product_name=product["name"],
            price_usdc=product["price_usdc"],
            amount_base=product["price_base"],
        ))

    total_amount_base = sum(i.amount_base for i in items)
    total_usdc = sum(i.price_usdc for i in items)

    order_id = uuid.uuid4().hex[:12]
    # reference: pubkey aleatoria única por orden
    reference = str(Pubkey.new_unique())
    order = Order(
        order_id=order_id,
        items=items,
        total_amount_base=total_amount_base,
        total_usdc=total_usdc,
        reference=reference,
        expires_at=time.time() + cfg.order_timeout_seconds,
    )
    async with ORDERS_LOCK:
        ORDERS[order_id] = order
        ORDERS_BY_REFERENCE[reference] = order_id

    transfer_request = payments.build_transfer_request(
        recipient=merchant_pubkey,
        amount_base=order.total_amount_base,
        mint=cfg.usdc_mint,
        reference=reference,
        include_meta=False,
        include_amount=False,
    )

    # QR en base64 para el HTML
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=4,
    )
    qr.add_data(transfer_request)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    logger.info(
        "Orden %s creada (ref=%s, %d items, total=%d base)",
        order_id, reference, len(items), total_amount_base,
    )
    return {
        "order_id": order_id,
        "items": [{"sku": i.sku, "product_name": i.product_name,
                   "price_usdc": i.price_usdc, "amount_base": i.amount_base}
                  for i in items],
        "total_amount_base": total_amount_base,
        "total_usdc": round(total_usdc, 2),
        "reference": reference,
        "transfer_request": transfer_request,
        "qr_base64": qr_b64,
        "timeout_s": cfg.order_timeout_seconds,
    }


@app.get("/order/{order_id}")
async def order_status(order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    return {
        "order_id": order.order_id,
        "status": order.status,
        "items": [{"sku": i.sku, "product_name": i.product_name,
                   "price_usdc": i.price_usdc, "amount_base": i.amount_base,
                   "dispensed": i.dispensed}
                  for i in order.items],
        "total_amount_base": order.total_amount_base,
        "total_usdc": round(order.total_usdc, 2),
        "reference": order.reference,
        "signature": order.signature,
        "paid_amount": order.paid_amount,
        "refund_status": order.refund_status,
        "refund_due_at": order.refund_due_at,
        "refund_amount_base": order.refund_amount_base,
        "refund_signature": order.refund_signature,
        "refund_error": order.refund_error,
        "remaining_s": order.remaining_s if order.status == "pendiente" else 0,
    }


@app.get("/order/{order_id}/qr")
async def order_qr(order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    transfer_request = payments.build_transfer_request(
        recipient=merchant_pubkey if merchant_pubkey else "",
        amount_base=order.total_amount_base,
        mint=cfg.usdc_mint,
        reference=order.reference,
        include_meta=False,
        include_amount=False,
    )
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=4,
    )
    qr.add_data(transfer_request)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/order/{order_id}/payment", response_class=HTMLResponse)
async def order_payment_page(request: Request, order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    transfer_request = payments.build_transfer_request(
        recipient=merchant_pubkey if merchant_pubkey else "",
        amount_base=order.total_amount_base,
        mint=cfg.usdc_mint,
        reference=order.reference,
        include_meta=False,
        include_amount=False,
    )
    return templates.TemplateResponse(
        request,
        "payment.html",
        {
            "order": order,
            "timeout_s": cfg.order_timeout_seconds,
            "transfer_request": transfer_request,
        },
    )


@app.get("/order/{order_id}/confirmed", response_class=HTMLResponse)
async def order_confirmed_page(request: Request, order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    if order.status not in ("pagada", "dispensada"):
        raise HTTPException(400, "La orden no está pagada")
    return templates.TemplateResponse(request, "confirmed.html", {"order": order})


@app.get("/order/{order_id}/expired", response_class=HTMLResponse)
async def order_expired_page(request: Request, order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    return templates.TemplateResponse(request, "error.html", {"order": order})


@app.post("/order/{order_id}/cancel")
async def cancel_order(order_id: str):
    """Cancela una orden pendiente (no se puede cancelar si ya se pagó)."""
    async with ORDERS_LOCK:
        order = ORDERS.get(order_id)
        if order is None:
            raise HTTPException(404, "Orden no encontrada")
        if order.status != "pendiente":
            raise HTTPException(400, f"No se puede cancelar: la orden está {order.status}")
        order.status = "cancelada"
        ORDERS_BY_REFERENCE.pop(order.reference, None)
    logger.info("Orden %s cancelada por el usuario", order_id)
    return {"order_id": order.order_id, "status": "cancelada"}


@app.get("/order/{order_id}/cancelled", response_class=HTMLResponse)
async def order_cancelled_page(request: Request, order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    return templates.TemplateResponse(request, "cancelled.html", {"order": order})


@app.get("/healthz")
async def healthz():
    rpc_ok = False
    if rpc_client is not None:
        try:
            ver = await rpc_client.get_version()
            rpc_ok = ver.value is not None
        except Exception:  # noqa: BLE001
            rpc_ok = False
    return {
        "status": "ok",
        "network": cfg.network,
        "rpc_ok": rpc_ok,
        "rpc_http": _resolve_rpc_http(),
        "wallet": str(merchant_pubkey) if merchant_pubkey else None,
        "signing": keypair is not None,
        "merchant_ata": str(merchant_ata) if merchant_ata else None,
        "usdc_mint": cfg.usdc_mint,
        "gpio": {
            "available": dispenser.is_available if dispenser else False,
            "mode": "stub" if (dispenser and dispenser.stub) else "real",
            "reason": (dispenser.stub_reason if dispenser else "dispenser no inicializado"),
            "dispense_pin": cfg.dispense_pin,
            "pulse_seconds": cfg.dispense_pulse_seconds,
        },
        "monitor": "running" if payment_monitor else "not_started",
        "pending_orders": len([o for o in ORDERS.values() if o.status == "pendiente"]),
        "auto_refund": {
            "enabled": cfg.auto_refund_enabled,
            "delay_s": cfg.refund_delay_seconds,
            "to": cfg.refund_to or None,
            "signing": keypair is not None,
        },
    }


@app.post("/api/test/dispense")
async def test_dispense(request: Request):
    """Prueba manual del dispensador. Requiere header X-Test-Token (TEST_TOKEN)."""
    token = [REDACTED]"X-Test-Token", "")
    if not cfg.test_token or token != cfg.test_token:
        raise HTTPException(403, "Token de prueba inválido o deshabilitado")
    body = await request.json()
    pin = int(body.get("pin", cfg.dispense_pin))
    seconds = float(body.get("seconds", cfg.dispense_pulse_seconds))
    dispenser.set_led("paid", True)
    await dispenser.dispense(pin, seconds)
    dispenser.set_led("paid", False)
    return {"ok": True, "pin": pin, "seconds": seconds}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)