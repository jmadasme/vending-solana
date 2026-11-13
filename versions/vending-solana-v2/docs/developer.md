# Developer — Vending Bebidas v2 (código fuente)

## Arquitectura

```
                         ┌─────────────────────────────────────┐
  Cliente (pantalla)     │  Raspberry Pi 5 · uvicorn :8001      │
  ── grilla + carrito ──▶│  app.py (FastAPI)                    │
  (index.html JS)        │    │  POST /order (items[])          │
                         │    │  GET /order/{id}/payment (QR)   │
                         │    ▼                                 │
                         │  payments.py  PaymentMonitor         │
                         │   ├─ WebSocket account_subscribe     │
                         │   └─ reconciliación cada 2 s         │
                         │    ▼                                 │
                         │  _on_payment → valida monto/ref      │
                         │    ▼                                 │
                         │  dispenser.py (GPIO 17)              │
                         │   dispense() 2.5 s por bebida        │
                         └─────────────────────────────────────┘
```

## Módulos

### `app.py` — web app FastAPI

Flujo de la v2 (multi-item):

- **`POST /order`** con body `{"items":[{"sku":"coca-cola"},{"sku":"sprite"}]}`:
  1. Throttle por IP (5 órdenes / 5 s).
  2. Valida cada sku contra `catalog.yaml`;
     si hay duplicados → 400; si el carrito está vacío → 400.
  3. **Recalcula el total** desde el catálogo:
     `total_amount_base = sum(item.amount_base)`.
     Nunca usa un total enviado por el cliente.
  4. Genera `reference = str(Pubkey.new_unique())`.
  5. Crea `Order` con `items: list[OrderItem]`, `total_amount_base`,
     `total_usdc`, `expires_at = now + cfg.order_timeout_seconds` (60 s).
  6. Construye el Transfer Request **sin amount**:
     `payments.build_transfer_request(..., include_meta=False, include_amount=False)`.
  7. Devuelve JSON: order_id, items, total, reference, transfer_request,
     qr_base64, timeout_s.

- **`_on_payment(payment)`** callback del monitor:
  1. Dentro de `ORDERS_LOCK`: busca orden pendiente cuyo `reference` esté en
     `payment.account_keys`.
  2. Si no, **empareja por monto** con `AMOUNT_TOLERANCE = 10_000` (±0.01 USDC)
     contra `order.total_amount_base` (no por item): toma la orden pendiente más
     antigua que coincida.
  3. Valida el monto; marca `pagada`, guarda `signature`/`paid_amount`.
  4. Fuera del lock: `_dispatch_dispense(order_id)`.

- **`_dispatch_dispense(order_id)`** — **dispensado secuencial**:
  ```python
  for item in order.items:
      await dispenser.dispense(cfg.dispense_pin, cfg.dispense_pulse_seconds)
      item.dispensed = True
      await asyncio.sleep(DISPENSE_BETWEEN_SECONDS)  # 1 s
  order.status = "dispensada"
  ```
  Si algo falla, `order.status = "pagada"` (reintentable) y LED error.

- **`_expiry_sweeper`**: cada 5 s marca como `expirada` las `pendiente` vencidas y
  libera el `reference` de `ORDERS_BY_REFERENCE`.

- **`POST /order/{id}/cancel`**: solo si `pendiente`; limpia el reference.

- **`GET /order/{id}/confirmed`**: requiere status `pagada` | `dispensada`.

- **`/healthz`**: red, RPC, wallet, ATA, mint, gpio (mode/reason), monitor.

### `payments.py` — Solana Pay (heredado de v1, aprobado)

- `build_transfer_request(recipient, amount_base, mint, reference, include_meta,
  include_amount)`:
  - `include_amount=False` omite `amount` (evita bug de parseo de Phantom).
  - `include_meta=False` omite `label`/`message` (QR más corto).
- `PaymentMonitor(rpc_http, rpc_ws, merchant_ata, on_payment,
  rpc_http_backup=None, reconcile_interval=2.0)`:
  - WebSocket `account_subscribe` + **reconciliación periódica** cada
    `reconcile_interval` (2.0 s en producción → detección < 2 s).
  - Failover a RPC de respaldo en `_rpc()`.
  - `validate_payment_tx` decodifica la transacción (`get_transaction`) y extrae
    `amount_base`, `reference`, `account_keys`. La instrucción esperada es
    `transfer_checked` (disc=12) del Token Program; `data[1:9]` = monto en base58.
- Fixed en v1: soporte para `ix.data` como **string base58** (transacciones
  reales de RPC con `encoding=json`), fallback a base64.

### `dispenser.py` — GPIO (heredado de v1, aprobado)

- `gpiozero` + `LGPIOFactory` (Pi 5/libgpiod). Modo *stub* automático en hosts sin
  GPIO (con `stub_reason` expuesto en `/healthz`).
- `dispense(pin, seconds)`: pulso activo-alto durante `seconds` (2.5 s).
- `set_led("ready"/"paid"/"error", bool)`: LEDs en pines 27/22/23.

### `wallet.py` (heredado)

- `load_or_create_keypair(wallet_path)`: carga/crea keypair (permisos 0600).
- `derive_ata(owner, mint)`, `get_or_create_ata(...)`: ATA USDC del comercio.

### `config.py` (heredado)

- Carga `config.yaml` + `.env` (prioridad: env > .env > config.yaml).

## Modelo de datos

```python
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
    total_amount_base: int   # suma de amount_base
    total_usdc: float        # suma de price_usdc
    reference: str
    status: str              # pendiente|pagada|dispensada|expirada|monto_incorrecto
    created_at / expires_at / signature / paid_amount
```

Estados: `pendiente` → `pagada` → `dispensada` (o `expirada` / `cancelada` /
`monto_incorrecto`).

## Frontend

- **`templates/index.html`**: layout 2 columnas (`.layout`).
  - Izquierda: `.grid` (4×2 tarjetas: imagen, nombre, precio, botón Añadir).
  - Derecha: `.cart` (sticky): lista, subtotales, TOTAL, Vaciar, Pagar.
  - JS: carrito en memoria `{sku: true}` (máx 1 por producto); al pagar hace
    `POST /order` y guarda el carrito en `localStorage('vending_cart')`.
- **`templates/payment.html`**: resumen visual del pedido (imagen de cada lata +
  nombre + precio), monto total protagonista (degradado), QR + **cronómetro
  circular** (SVG, 340×340 a la izquierda del QR) con cuenta regresiva
  sincronizada con el tiempo real (`remaining_s` del servidor, tick 250 ms;
  ≤10 s se pone rojo; al llegar a 0 redirige a `/`), y botón cancelar.
- **`templates/confirmed.html`**: celebración con confeti, lista de bebidas
  dispensadas; **limpia `localStorage('vending_cart')`** y redirige a `/` en 15 s.
- **`static/style.css`**: tema dark premium (violeta #9945FF, menta #14F195),
  fondo oscuro uniforme (sin gradientes de color en esquinas), layout
  grilla+carrito responsive, tarjetas compactas 4×2, animaciones.
- **`static/*.png`**: 8 latas generadas con Pillow (script `/tmp/opencode/make_cans_v2.py`).

## Endpoints

| Método | Ruta | Uso |
|---|---|---|
| GET | `/` | Catálogo + carrito |
| POST | `/order` | Crear orden multi-item |
| GET | `/order/{id}` | Estado (polling) |
| GET | `/order/{id}/qr` | PNG del QR |
| GET | `/order/{id}/payment` | Página de pago |
| GET | `/order/{id}/confirmed` | Celebración |
| GET | `/order/{id}/expired` | Expirada |
| POST | `/order/{id}/cancel` | Cancelar (solo pendiente) |
| GET | `/order/{id}/cancelled` | Cancelada |
| GET | `/healthz` | Salud |
| POST | `/api/test/dispense` | Test relé (X-Test-Token) |

## Scripts

| Script | Descripción |
|---|---|
| `scripts/sales.py` | Visor solo-lectura de saldo ATA y transacciones (`--watch`, `--last N`) |
| `scripts/refund.py` | Reembolso: `--to wallet --amount USDC --yes` (usa `TransferCheckedParams` de `spl.token.models`; crea ATA destino si falta) |
| `scripts/dispense_test.py` | Prueba de pulso GPIO |
| `scripts/devnet_e2e_test.py` | Test E2E on-chain (devnet) |
| `scripts/setup_wallet.py` | Crear/fondear keypair + ATA |

## Cómo extender

- **Nueva bebida**: añade la entrada en `catalog.yaml` (sku, nombre, price_usdc,
  price_base, pulse_seconds, dispense_pin, image) y coloca la imagen en `static/`.
- **Precio**: edita `catalog.yaml` y reinicia (`sudo systemctl restart vending-v2`).
- **Multiple motores**: el schema ya soporta `dispense_pin` por producto; cambiar
  `_dispatch_dispense` para usar `item`-specific pin.
- **Quitar límite de 1 por producto**: quitar la validación de duplicados en
  `POST /order` y adaptar el JS del carrito.