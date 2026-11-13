# app.py — Web app FastAPI de la máquina expendedora cripto.
"""Punto de entrada (uvicorn app:app). Endpoints:

- GET  /                      -> catálogo
- POST /order                 -> crea orden + Transfer Request + QR
- GET  /order/{order_id}      -> estado de la orden (JSON, polling)
- GET  /order/{order_id}/qr   -> PNG del QR Solana Pay
- GET  /order/{order_id}/confirmed -> HTML "pago confirmado"
- GET  /order/{order_id}/expired   -> HTML "orden expirada"
- GET  /healthz               -> salud (RPC + GPIO + wallet)
- POST /api/test/dispense     -> prueba manual del dispensador (TEST_TOKEN)

El backend escucha pagos por WebSocket (account_subscribe) y valida monto +
reference server-side antes de disparar el relé/motor (pulso 2.5 s).
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
from config import load_config
from dispenser import Dispenser
from wallet import derive_ata, get_or_create_ata, load_or_create_keypair

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
class CreateOrderRequest(BaseModel):
    sku: str


@dataclass
class Order:
    order_id: str
    sku: str
    product_name: str
    price_usdc: float
    amount_base: int
    reference: str
    status: str = "pendiente"  # pendiente|pagada|dispensada|expirada|monto_incorrecto
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    signature: Optional[str] = None
    paid_amount: Optional[int] = None
    dispensed: bool = False

    @property
    def remaining_s(self) -> int:
        return max(0, int(self.expires_at - time.time()))


ORDERS: dict[str, Order] = {}
ORDERS_LOCK = asyncio.Lock()
ORDERS_BY_REFERENCE: dict[str, str] = {}

# Pagos recibidos sin una orden pendiente asociada (para devolución manual).
# Formato: lista de dicts con signature, amount_base, tx_time.
UNMATCHED_PAYMENTS: list[dict] = []


# ----------------------------------------------------------------------
# Dependencias de la app (wallet, ATA, dispensador, monitor)
# ----------------------------------------------------------------------
keypair: Optional[Keypair] = None
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
    global keypair, merchant_ata, payment_monitor, rpc_client
    from solana.rpc.async_api import AsyncClient

    rpc_client = AsyncClient(_resolve_rpc_http())
    keypair = load_or_create_keypair(cfg.wallet_abs_path)
    mint = Pubkey.from_string(cfg.usdc_mint)
    # Derivación siempre disponible; la creación on-chain necesita SOL (rent).
    merchant_ata = derive_ata(keypair.pubkey(), mint)
    try:
        created_ata, _ = await get_or_create_ata(rpc_client, keypair, keypair.pubkey(), mint)
        merchant_ata = created_ata
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo crear la ATA on-chain (%s). Usando ATA derivada.", exc)
    logger.info("Wallet comercio: %s", keypair.pubkey())
    logger.info("ATA USDC comercio: %s (mint %s)", merchant_ata, mint)

    payment_monitor = payments.PaymentMonitor(
        rpc_http=_resolve_rpc_http(),
        rpc_ws=cfg.rpc_ws,
        merchant_ata=merchant_ata,
        on_payment=_on_payment,
        rpc_http_backup=_rpc_http_backup(),
        # Detección casi instantánea: el fallback de reconciliación corre cada
        # 2s (no 30s) para que, aunque el WebSocket account_subscribe del RPC
        # público no entregue notificaciones a tiempo, el pago se detecte en
        # ~2s en vez de ~15s.
        reconcile_interval=2.0,
    )
    await payment_monitor.start()


# Tolerancia de monto en unidades base (6 decimales): ±0.01 USDC.
# Necesaria porque wallets como Phantom pueden ajustar el amount ligeramente
# (p.ej. 1500192 en vez de 1500000) o no incluir el `reference` al digitar
# el monto manualmente.
AMOUNT_TOLERANCE = 10_000  # 0.01 USDC


async def _on_payment(payment: payments.Payment) -> None:
    """Callback del monitor: valida contra las órdenes pendientes y dispensa."""
    logger.info(
        "on_payment: amount=%d sig=%s keys=%s",
        payment.amount_base,
        payment.signature,
        (payment.account_keys or [])[:6],
    )
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
            pending_by_amount = [
                o for o in ORDERS.values()
                if o.status == "pendiente"
                and abs(o.amount_base - payment.amount_base) <= AMOUNT_TOLERANCE
            ]
            if pending_by_amount:
                pending_by_amount.sort(key=lambda o: o.created_at)
                order = pending_by_amount[0]
                logger.info(
                    "Emparejado por monto (sin reference): orden %s esperaba %d, "
                    "recibido %d (tolerancia %d)",
                    order.order_id, order.amount_base, payment.amount_base,
                    AMOUNT_TOLERANCE,
                )

        if order is None:
            logger.warning("Pago sin orden pendiente asociada (monto=%d)", payment.amount_base)
            # I-005: registrar para devolución manual.
            UNMATCHED_PAYMENTS.append({
                "signature": payment.signature,
                "amount_base": payment.amount_base,
                "tx_time": payment.tx_time,
                "recorded_at": time.time(),
            })
            return

        # 3) Validar monto dentro de tolerancia
        if abs(payment.amount_base - order.amount_base) > AMOUNT_TOLERANCE:
            order.status = "monto_incorrecto"
            order.paid_amount = payment.amount_base
            logger.warning(
                "Monto incorrecto para orden %s: esperado %d, recibido %d",
                order.order_id, order.amount_base, payment.amount_base,
            )
            return

        order.status = "pagada"
        order.signature = payment.signature
        order.paid_amount = payment.amount_base
        order_id = order.order_id

    # 3) Dispensar (fuera del lock)
    logger.info("Orden %s PAGADA -> dispensando…", order_id)
    dispenser.set_led("paid", True)
    try:
        await dispenser.dispense()
        order.dispensed = True
        order.status = "dispensada"
        logger.info("Orden %s DISPENSADA", order_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Fallo al dispensar orden %s: %s", order_id, exc)
        order.status = "pagada"  # pagado pero sin dispensar (reintentable)
        dispenser.set_led("error", True)
    finally:
        dispenser.set_led("paid", False)


async def _expiry_sweeper() -> None:
    """Marca como expiradas las órdenes sin pagar tras el timeout."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        async with ORDERS_LOCK:
            for order in list(ORDERS.values()):
                if order.status == "pendiente" and now > order.expires_at:
                    order.status = "expirada"
                    # I-005: liberar el reference al expirar la orden.
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


app = FastAPI(title="Vending Solana", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(cfg.base_dir / "static")), name="static")


# ----------------------------------------------------------------------
# Middleware de seguridad (I-006)
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


# Throttle simple por IP para POST /order (I-006).
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
    # I-006: throttle simple por IP.
    client_ip = request.client.host if request.client else "unknown"
    _throttle_order(client_ip)

    product = PRODUCTS_BY_SKU.get(req.sku)
    if product is None:
        raise HTTPException(404, f"Producto desconocido: {req.sku}")
    if keypair is None or merchant_ata is None:
        raise HTTPException(503, "Backend no inicializado")

    order_id = uuid.uuid4().hex[:12]
    # reference: pubkey aleatoria única por orden
    reference = str(Pubkey.new_unique())
    order = Order(
        order_id=order_id,
        sku=product["sku"],
        product_name=product["name"],
        price_usdc=product["price_usdc"],
        amount_base=product["price_base"],
        reference=reference,
        expires_at=time.time() + cfg.order_timeout_seconds,
    )
    async with ORDERS_LOCK:
        ORDERS[order_id] = order
        ORDERS_BY_REFERENCE[reference] = order_id

    transfer_request = payments.build_transfer_request(
        recipient=keypair.pubkey(),
        amount_base=order.amount_base,
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

    logger.info("Orden %s creada (ref=%s, amount=%d)", order_id, reference, order.amount_base)
    return {
        "order_id": order_id,
        "sku": product["sku"],
        "product_name": order.product_name,
        "amount_base": order.amount_base,
        "price_usdc": order.price_usdc,
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
        "product_name": order.product_name,
        "amount_base": order.amount_base,
        "price_usdc": order.price_usdc,
        "reference": order.reference,
        "signature": order.signature,
        "paid_amount": order.paid_amount,
        "remaining_s": order.remaining_s if order.status == "pendiente" else 0,
    }


@app.get("/order/{order_id}/qr")
async def order_qr(order_id: str):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(404, "Orden no encontrada")
    transfer_request = payments.build_transfer_request(
        recipient=keypair.pubkey() if keypair else "",
        amount_base=order.amount_base,
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
        recipient=keypair.pubkey() if keypair else "",
        amount_base=order.amount_base,
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
    import json

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
        "wallet": str(keypair.pubkey()) if keypair else None,
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