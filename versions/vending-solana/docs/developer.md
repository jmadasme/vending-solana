# Documentación técnica — Vending Solana

> Explicación del código fuente, arquitectura de módulos, flujo lógico y guía de
> extensión para desarrolladores.

---

## 1. Arquitectura de módulos

```
vending-solana/
├── app.py         # Punto de entrada FastAPI: endpoints + ciclo de vida de la orden
├── config.py      # Carga de configuración (env > .env > config.yaml)
├── wallet.py      # Keypair del comercio + derivación/creación de ATA USDC
├── payments.py    # Solana Pay + account_subscribe + validación + reconciliación
├── dispenser.py   # Control GPIO del relé/motor + LEDs (gpiozero/lgpio)
├── catalog.yaml   # Productos
├── config.yaml    # Configuración (mainnet-beta)
├── config.devnet.yaml.bak  # Respaldo de la configuración devnet original
├── templates/     # index.html, payment.html, confirmed.html,
│                  #   cancelled.html, error.html
├── static/        # style.css (tema dark premium), coca-cola.png (512×512 RGBA)
└── scripts/
    ├── setup_wallet.py     # Keypair + ATA (+ airdrop en devnet)
    ├── dispense_test.py    # Test manual de pulso GPIO
    ├── devnet_e2e_test.py  # E2E autónomo en devnet
    └── sales.py            # Visor de ventas SOLO-LECTURA (consola)
```

**Flujo de datos general:**

```
Teléfono ──HTTP──▶ app.py ──▶ payments.py (Transfer Request + QR)
                              │
                              └──▶ WebSocket account_subscribe ──▶ RPC mainnet-beta
                                        │ (notificación)
                                        ▼
                              payments.validate_payment_tx()
                                        │ (Payment válido)
                                        ▼
                              app._on_payment() ──▶ dispenser.dispense() ──▶ GPIO 17

Terminal del operador ──▶ scripts/sales.py (solo lectura, sin puertos ni keys)
```

---

## 2. Módulo `config.py`

### Propósito

Carga la configuración desde tres fuentes con prioridad:
**variables de entorno > `.env` > `config.yaml`** (los valores por defecto de la
dataclass `Config` quedan al final).

### API

| Símbolo | Descripción |
|---|---|
| `Config` | Dataclass con todos los parámetros (network, RPC, mint, pines, timeouts, etc.) |
| `load_config()` | Devuelve una instancia `Config` resuelta |
| `Config.wallet_abs_path` | Propiedad: ruta absoluta del keypair (resuelve relativo a `base_dir`) |

### Detalles

- Usa `python-dotenv` (`load_dotenv(BASE_DIR / ".env")`).
- Lee `config.yaml` y asigna solo claves conocidas (ignora `base_dir`).
- Aplica las variables de entorno mapeadas en `env_map` (p. ej. `RPC_HTTP` →
  `rpc_http`), con coerción de tipos (`int`/`float`) para `usdc_decimals`,
  `order_timeout_seconds`, `dispense_pulse_seconds` y `dispense_pin`.
- `BASE_DIR` se fija como el directorio del propio archivo, independientemente
  del directorio de trabajo.

> **Nota de red:** los *defaults* de la dataclass `Config` son `devnet`, pero
> `config.yaml` (y el `.env` de la Pi) los sobrescriben a **mainnet-beta** con el
> USDC real. El código no decide la red; la decide la configuración cargada.

### Cómo añadir un parámetro

1. Añade el campo a la dataclass `Config` con su valor por defecto.
2. (Opcional) Añádelo a `config.yaml`.
3. (Opcional) Añade el mapeo en `env_map` si debe venir de entorno.

---

## 3. Módulo `wallet.py`

### Propósito

Gestiona el keypair del comercio y su ATA de USDC, **sin exponer la clave
privada** fuera del módulo (solo devuelve `pubkey` y dirección ATA).

### API

| Función | Firma | Descripción |
|---|---|---|
| `load_or_create_keypair` | `(path) -> Keypair` | Carga el keypair (base58 o lista de bytes) o crea uno nuevo con permisos `0600` |
| `derive_ata` | `(owner: Pubkey, mint: Pubkey) -> Pubkey` | Deriva la ATA (PDA) sin crearla on-chain |
| `get_or_create_ata` | `(client, payer, owner, mint) -> (Pubkey, bool)` | Crea la ATA on-chain si no existe (devuelve `(ata, created)`) |

### Detalles

- Formato del keypair: JSON estándar de Solana (lista de enteros), compatible
  con `solders.Keypair.from_bytes`. También acepta string base58.
- `MINT_ACCOUNT_SPACE = 82` (tamaño de cuenta mint para rent-exemption).
- La creación de ATA usa `create_associated_token_account` y `TOKEN_PROGRAM_ID`
  del namespace `spl.token` (empaquetado en `solana>=0.40`), compilando un
  `MessageV0` con `VersionedTransaction`.

---

## 4. Módulo `dispenser.py`

### Propósito

Control del relé/motor y los LEDs de estado mediante **gpiozero con backend
`lgpio`** (compatible Pi 5). Si no hay GPIO disponible (host de desarrollo),
degrada a **modo stub** y registra eventos en el log sin tocar pines.

### API

| Símbolo | Descripción |
|---|---|
| `Dispenser(config, force_stub=False)` | Controlador principal |
| `Dispenser.is_available` | `True` si el GPIO está operativo |
| `Dispenser.stub` / `Dispenser.stub_reason` | Modo y motivo de la degradación |
| `Dispenser.set_led(name, on)` | Enciende/apaga un LED (`ready`/`paid`/`error`) |
| `Dispenser.dispense(pin=None, seconds=None)` | **async**: pulso del relé (por defecto pin 17, 2.5 s) |
| `Dispenser.dispense_sync(...)` | Variante síncrona (para CLI/tests) |
| `Dispenser.cleanup()` | Apaga todo y libera pines |
| `dispense(pin, seconds)` | Helper CLI de un solo uso |

### Detalles

- `_init_gpio()`: si `dispenser_mode == "stub"` o `gpiozero`/`lgpio` no están
  disponibles, activa `stub` con motivo explicativo. Usa `LGPIOFactory()` que
  falla si no hay `/dev/gpiochip`.
- `_dispense_blocking(pin, seconds)`: ejecuta el pulso en un hilo
  (`asyncio.to_thread`), con `threading.Lock` anti-doble-dispensado
  (`_dispensing`). Enciende el relé, espera `seconds`, apaga.
- En modo stub el `sleep` se limita a 0.5 s para no bloquear las pruebas.

---

## 5. Módulo `payments.py`

### Propósito

Núcleo de la detección de pagos: construcción del **Transfer Request Solana
Pay**, escucha por **`account_subscribe`** (WebSocket), **validación** de monto +
`reference`, y **resiliencia** (reconexión con backoff, reconciliación periódica
y failover RPC).

### API

| Símbolo | Descripción |
|---|---|
| `Payment` | Dataclass: `signature`, `reference`, `amount_base`, `merchant_ata`, `tx_time`, `account_keys` |
| `build_transfer_request(recipient, amount_base, mint, reference, label, message, include_amount=True, include_meta=True) -> str` | Construye la URL `solana:...` |
| `validate_payment_tx(tx, merchant_ata, reference, allow_any_reference=False) -> Optional[Payment]` | Valida una transacción como pago a la ATA |
| `PaymentMonitor(...)` | Monitor con reconciliación periódica (detección < 2 s) y failover |

### `build_transfer_request`

Devuelve:

```
solana:<recipient>?spl-token=[REDACTED]
```

- **`include_amount=False` en producción**: el QR **no lleva `amount`** porque
  badges (p. ej. Phantom) parsean mal el monto y muestran cifras absurdas
  (150000 en vez de 1.00). El monto se muestra en la pantalla y el cliente lo
  digita al confirmar.
- Los campos opcionales `label` y `message` se omiten en producción
  (`include_meta=False`) para acortar la URL del QR y evitar que el wallet los
  ignore. Se escapan con `urllib.parse.quote` si se incluyen.
- El `reference` **siempre viaja**: es imprescindible para el emparejamiento por
  QR (aunque Phantom no lo incluya al digitar el monto, el emparejamiento por
  monto lo sustituye — ver §6).

### `validate_payment_tx`

- Itera las instrucciones compiladas del mensaje (v0 o legacy) y resuelve el
  `program_id` de cada una contra `account_keys`.
- Si el programa es `TOKEN_PROGRAM_ID` y la instrucción es un `transfer`
  (discriminador `3`) o `transfer_checked` (discriminador `12`) cuyo destino es
  la `merchant_ata`, extrae el `amount` (u64 LE en `data[1:9]`).
- Si se pasa `reference`, exige que su pubkey esté entre las `account_keys`.
- Devuelve un `Payment` si hay una transferencia válida con monto > 0.

> `sales.py` reutiliza `validate_payment_tx(..., allow_any_reference=True)` para
> decodificar ventas históricas sin exigir un `reference` concreto.

### `PaymentMonitor`

Atributos clave: `rpc_http`, `rpc_ws`, `rpc_http_backup`, `merchant_ata`,
`on_payment` (callback async), `_seen_signatures` (set), `_baseline` (int).

**Métodos principales:**

| Método | Descripción |
|---|---|
| `start()` / `stop()` | Arranca/detiene la tarea `_run()` |
| `_run()` | Bucle de suscripción con backoff exponencial (máx. 30 s) |
| `_reconcile()` / `_reconcile_with_client()` | Fija el balance base y procesa pagos recientes al (re)conectar |
| `_periodic_reconcile_loop()` | Re-consulta firmas cada `reconcile_interval` (2.0 s en producción) **sin gate de balance** |
| `_handle_notification()` | Procesa cada notificación de cambio en la ATA |
| `_process_new_signatures_from_balance()` | Lee balance y procesa firmas nuevas (re-consulta aunque el balance no cambie) |
| `_process_new_signatures()` | Consulta `get_signatures_for_address` (limit 10) y valida las no vistas |
| `_inspect_signature()` | `get_transaction` con reintentos (backoff) y validación |
| `_rpc(fn)` | Ejecuta `fn(client)` sobre el RPC primario; ante fallo, failover al respaldo |

### Ciclo de vida de la detección (núcleo)

```
_run()
 └─ _reconcile()                          # balance base + firmas recientes
 └─ connect(rpc_ws)
     └─ account_subscribe(merchant_ata, confirmed, base64)
     └─ _periodic_reconcile_loop()        # cada 2 s re-escanea firmas
     └─ loop de mensajes:
          └─ notificación → _handle_notification()
               └─ _rpc(_process_new_signatures_from_balance)
                    └─ get_token_account_balance + _process_new_signatures
                         └─ get_signatures_for_address (limit 10)
                              └─ por cada firma no vista:
                                   _inspect_signature(sig)
                                   └─ get_transaction (con reintentos)
                                        └─ validate_payment_tx()
                                        └─ on_payment(payment)  # callback a app.py
```

### Decisiones de diseño importantes

- **Firma solo vista tras validación.** `_seen_signatures.add(sig)` ocurre
  **únicamente** tras `_inspect_signature() == True`. Así, un fallo transitorio
  de `get_transaction` **no descarta** la firma: la siguiente ronda periódica
  (o notificación/reconexión) la reintenta. No hay cola de reintento propia.
- **Reintentos con backoff.** `_inspect_signature` reintenta `get_transaction`
  (`get_transaction_retries`, 3) con `0.5 * 2^attempt` s. Si agota reintentos,
  **propaga** la excepción para que `_rpc` active el failover al RPC de respaldo.
- **Failover RPC.** `_rpc(fn)` ejecuta contra el primario y, si falla, contra
  `rpc_http_backup` (si es distinto del primario).
- **Compatibilidad solana-py.** Captura `TypeError` en `get_transaction` para
  degradar si la versión no acepta `max_supported_transaction_version`.

---

## 6. Módulo `app.py`

### Propósito

Web app FastAPI: define los endpoints, mantiene el estado de las órdenes en
memoria, orquesta el ciclo de vida (incluida la **cancelación**) y dispara el
dispensador.

### Estado global

| Variable | Descripción |
|---|---|
| `ORDERS: dict[str, Order]` | Órdenes por `order_id` |
| `ORDERS_BY_REFERENCE: dict[str, str]` | Mapa `reference → order_id` (1:1) |
| `UNMATCHED_PAYMENTS: list[dict]` | Pagos sin orden pendiente (devolución manual) |
| `ORDERS_LOCK` | `asyncio.Lock` para acceso concurrente |
| `keypair`, `merchant_ata`, `dispenser`, `payment_monitor`, `rpc_client` | Dependencias globales inicializadas en el lifespan |

### Dataclass `Order`

Campos: `order_id`, `sku`, `product_name`, `price_usdc`, `amount_base`,
`reference`, `status`, `created_at`, `expires_at`, `signature`, `paid_amount`,
`dispensed`. Propiedad `remaining_s` (segundos restantes).

Estados posibles de `status`:
`pendiente | pagada | dispensada | expirada | cancelada | monto_incorrecto`.

### Endpoints

| Ruta | Función | Detalle |
|---|---|---|
| `GET /` | `index` | Render catálogo (`index.html`) |
| `POST /order` | `create_order` | Crea orden + Transfer Request + QR base64 |
| `GET /order/{id}` | `order_status` | Estado JSON (polling) |
| `GET /order/{id}/qr` | `order_qr` | PNG del QR |
| `GET /order/{id}/payment` | `order_payment_page` | HTML pago (QR + botón cancelar) |
| `POST /order/{id}/cancel` | `cancel_order` | Cancela una orden `pendiente` (rechazada si ya se pagó) |
| `GET /order/{id}/cancelled` | `order_cancelled_page` | HTML "orden cancelada" |
| `GET /order/{id}/confirmed` | `order_confirmed_page` | HTML confirmado (solo si pagada/dispensada) |
| `GET /order/{id}/expired` | `order_expired_page` | HTML expirada |
| `GET /healthz` | `healthz` | Salud (RPC, GPIO, wallet, monitor) |
| `POST /api/test/dispense` | `test_dispense` | Dispensado manual (requiere `X-Test-Token`) |

### Flujo de la orden (`create_order`)

1. Throttle por IP (5 órdenes/5 s) — headers de seguridad en middleware.
2. Busca el producto por `sku` en `catalog.yaml`.
3. Genera `order_id` (uuid 12 hex) y `reference = Pubkey.new_unique()`.
4. Guarda la orden y el mapa `reference → order_id` bajo `ORDERS_LOCK`.
5. Construye el Transfer Request y el QR (base64 PNG).
6. Devuelve JSON con `order_id`, `reference`, `transfer_request`, `qr_base64`,
   `timeout_s`.

### Cancelación (`cancel_order`)

```python
@app.post("/order/{order_id}/cancel")
async def cancel_order(order_id: str):
    async with ORDERS_LOCK:
        order = ORDERS.get(order_id)
        if order is None:
            raise HTTPException(404, "Orden no encontrada")
        if order.status != "pendiente":
            raise HTTPException(400, f"No se puede cancelar: la orden está {order.status}")
        order.status = "cancelada"
        ORDERS_BY_REFERENCE.pop(order.reference, None)
    return {"order_id": order.order_id, "status": "cancelada"}
```

Puntos clave:

- **Solo en `pendiente`.** Si la orden está `pagada`/`dispensada`, responde
  `400`. Esto evita dispensar sin cobro en una carrera pago/cancelación.
- **Limpieza de `ORDERS_BY_REFERENCE`.** Al cancelar (igual que al expirar) se
  libera el `reference` para que un pago tardío no se asocie a la orden.
- **Sin confirmación.** La UI (`payment.html`) llama directamente al endpoint
  (botón `btn-danger`), sin diálogo de confirmación.
- La página `cancelled.html` se sirve en `GET /order/{id}/cancelled`.

### Callback `_on_payment(payment)`

Ejecutado por el monitor ante un pago válido:

1. Bajo `ORDERS_LOCK`, busca la orden **pendiente** cuyo `reference` esté entre
   las `account_keys` de la transacción.
2. Si no hay orden, registra en `UNMATCHED_PAYMENTS` (devolución manual).
3. Valida **monto exacto**; si difiere, marca `monto_incorrecto` y no dispensa.
4. Si coincide, marca `pagada` y guarda la firma/monto.
5. Fuera del lock, dispara el dispensador: `dispenser.dispense()` → `dispensada`,
   o `pagada` (reintentable) + LED error si falla.

> Una orden `cancelada` no aparece como `pendiente`, por lo que `_on_payment` la
> ignora (su `reference` ya no está en `ORDERS_BY_REFERENCE`).

### `_expiry_sweeper`

Tarea en segundo plano (cada 5 s) que marca como `expirada` las órdenes
pendientes cuyo `expires_at` pasó, y libera su entrada en `ORDERS_BY_REFERENCE`.

### Lifespan

- Arranque: crea `Dispenser`, inicializa el backend (`_init_backend`: cliente
  RPC, keypair, ATA, `PaymentMonitor.start()`), y lanza `_expiry_sweeper`.
- Parada: cancela el sweeper, detiene el monitor, cierra el cliente RPC y hace
  `dispenser.cleanup()`.

### Middleware de seguridad

- Headers: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, CSP básico.
- Throttle por IP en `POST /order` (ventana 5 s, máx. 5 órdenes).

---

## 7. Diagrama de flujo (lógica principal)

```
                 ┌───────────────────────────────┐
                 │  Cliente toca "Comprar"       │
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │ POST /order                   │
                 │  · throttle por IP            │
                 │  · crea Order + reference     │
                 │  · Transfer Request + QR      │
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │ Cliente escanea QR y firma    │
                 │ (Phantom/Solflare)            │
                 └───────────────┬───────────────┘
                                 ▼
        ┌────────────────────────────────────────────┐
        │ PaymentMonitor (WebSocket account_subscribe)│
        │  · notificación de cambio en la ATA        │
        │  · get_signatures_for_address + get_tx     │
        │  · validate_payment_tx (monto + ref)       │
        └───────────────────┬────────────────────────┘
                            ▼
                 ┌───────────────────────────────┐
                 │ app._on_payment(payment)      │
                 │  · ¿reference coincide?       │──no──▶ UNMATCHED_PAYMENTS
                 │  · ¿monto exacto?             │──no──▶ monto_incorrecto
                 │  · status = pagada            │
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │ dispenser.dispense()          │
                 │  · relé GPIO 17 ON 2.5 s      │
                 │  · LEDs (paid)                │
                 └───────────────┬───────────────┘
                                 ▼
                 ┌───────────────────────────────┐
                 │ status = dispensada           │
                 │ Cliente ve "Retira producto"  │
                 └───────────────────────────────┘

  [Rama de cancelación — en paralelo mientras "pendiente"]
                 ┌───────────────────────────────┐
                 │ POST /order/{id}/cancel       │
                 │  · ¿status == pendiente?      │──no──▶ 400 (rechazada)
                 │  · status = cancelada         │
                 │  · pop(ORDERS_BY_REFERENCE)   │
                 └───────────────┬───────────────┘
                                 ▼
                    GET /order/{id}/cancelled
                    Cliente ve "Orden cancelada"
```

---

## 8. Guía de modificación y extensión

### Añadir un producto

Edita `catalog.yaml`:

```yaml
products:
  - sku: coca-cola
    name: "Coca-Cola (lata 330 ml)"
    price_usdc: 1.00
    price_base: 1000000     # 1.00 × 10^6
    pulse_seconds: 2.5      # duración del pulso del relé
    dispense_pin: 17        # pin del relé para este producto
    image: "coca-cola.png"  # archivo en static/
  - sku: agua
    name: "Agua mineral (500 ml)"
    price_usdc: 0.80
    price_base: 800000
    pulse_seconds: 2.0
    dispense_pin: 17
    image: "agua.png"
```

> `price_base = round(price_usdc * 10**usdc_decimals)`. Añade la imagen
> correspondiente en `static/` o elimina el campo `image`.

### Cambiar de red (mainnet ↔ devnet)

El proyecto ya está en **mainnet-beta** (`config.yaml`). Para alternar:

- **Volver a devnet:** renombra `config.devnet.yaml.bak` a `config.yaml` (mint de
  Circle faucet `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`). En la Pi, además
  ajusta el `.env` si sobrescribe RPC/mint.
- **Pasar a mainnet (producción):**
  1. En `config.yaml` (o `.env`):
     - `network: mainnet-beta`
     - `rpc_http` / `rpc_ws`: endpoint mainnet (público o Helius).
     - `usdc_mint: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (USDC real).
  2. Usa un **keypair de producción** con fondos reales (SOL para el rent de la
     ATA + USDC).
  3. Habilita un RPC de respaldo y considera Helius para mayor robustez.
  4. **Prueba exhaustivamente en devnet antes de pasar a mainnet** (mainnet =
     dinero real).

### Añadir un token distinto (distinto mint)

- Cambia `usdc_mint` (y `usdc_decimals`) en `config.yaml`/`.env`.
- El código es agnóstico del mint: cualquier token SPL con `transfer` /
  `transfer_checked` hacia la ATA del comercio se detecta. Para pruebas
  autónomas, `scripts/devnet_e2e_test.py` crea su propio mint y lo inyecta por
  variable `USDC_MINT`.

### Añadir un endpoint

1. Define la ruta con `@app.get(...)` / `@app.post(...)`.
2. Usa `templates.TemplateResponse` para HTML o `JSONResponse` para JSON.
3. Respeta el patrón de lock (`ORDERS_LOCK`) al tocar `ORDERS` /
   `ORDERS_BY_REFERENCE`.

### Ajustar la detección / reconciliación

- `reconcile_interval` (**2.0 s en producción**, fijado en `app.py` al construir
  el `PaymentMonitor`) y `get_transaction_retries` (3) son parámetros del
  constructor. Con RPC público, este re-escaneo periódico es el mecanismo de
  detección principal (objetivo < 2 s verificado en producción).
- `max_backoff` (30 s) controla el tope de reconexión del WebSocket.

### Re-tematizar la interfaz

- Edita `static/style.css`: las variables de color viven en `:root` (`--violet`,
  `--mint`, `--bg-*`, `--danger`, etc.).
- La imagen de producto `static/coca-cola.png` es un PNG 512×512 RGBA con fondo
  transparente; sustitúyelo por cualquier PNG transparente del mismo estilo.

---

## 9. Tests y scripts

### `scripts/setup_wallet.py`

- Carga/crea el keypair del comercio.
- Airdroppea SOL devnet si el balance es bajo (< 2 SOL) — **solo útil en
  devnet**; en mainnet debes fondear manualmente.
- Crea la ATA de USDC y muestra `MERCHANT_PUBKEY`, `MERCHANT_ATA`,
  `USDC_MINT`, `USDC_BALANCE_BASE`.

```bash
python scripts/setup_wallet.py
```

### `scripts/dispense_test.py`

- Pulso manual del relé en el pin indicado (por defecto 17, 2.5 s).

```bash
python scripts/dispense_test.py --pin 17 --seconds 2.5 --force-stub
```

### `scripts/devnet_e2e_test.py`

Prueba **end-to-end autónoma** (sin faucet de Circle):

1. Crea un token SPL propio (6 decimales) en devnet.
2. Airdroppea SOL al comercio de test y al comprador.
3. Arranca la app (`uvicorn`) con `USDC_MINT` = token de prueba y
   `WALLET_PATH` propio, `DISPENSER_MODE=stub`.
4. Crea una orden por HTTP y valida el Transfer Request.
5. Paga con `transfer_checked` incluyendo el `reference` (como Phantom).
6. Verifica que la app detecta el pago (estado `pagada`/`dispensada`).
7. Prueba el endpoint `/api/test/dispense` y `healthz`.

```bash
python scripts/devnet_e2e_test.py --port 8199
```

### `scripts/sales.py` (visor de ventas)

**Solo lectura:** consulta el balance de la ATA y las transacciones recientes
para mostrar las ventas. **No abre puertos, no escucha, no usa claves privadas.**
Deriva la ATA a partir de `wallet.json` (sin exponer la key) o pide la dirección
interactivamente.

```bash
.venv/bin/python scripts/sales.py          # balance + últimas 15 transacciones
.venv/bin/python scripts/sales.py --watch  # vigilancia en vivo (cada 5 s)
.venv/bin/python scripts/sales.py --last 20
```

Reutiliza `config.yaml` (RPC/red), `wallet.derive_ata` y
`payments.validate_payment_tx(..., allow_any_reference=True)` para decodificar
cada firma como venta.

### `scripts/refund.py` (reembolso)

Devuelve USDC desde la ATA del comercio a una wallet destino (pruebas, devolución
o reposición de saldo):

```bash
.venv/bin/python scripts/refund.py --to <PUBKEY_DESTINO> --amount 1.00 --yes
```

- Construye una `transfer_checked` SPL con `TransferCheckedParams` (importado de
  `spl.token.models`).
- Crea la ATA del destino si no existe (el rent lo paga la wallet del comercio).
- No es una venta: no lleva `reference`, por lo que el monitor no la asocia a
  ninguna orden.

---

## 10. Notas de implementación

- **`spl.token` dentro de `solana>=0.40`.** El namespace `spl.token.constants` y
  `spl.token.instructions` se distribuye empaquetado con `solana`. No instalar
  paquetes `spl-*` externos (`spl-token-py` es malicioso).
- **Pi 5 / libgpiod.** La Pi 5 usa `/dev/gpiochip4`; se controla con
  `gpiozero` + backend `lgpio` (`LGPIOFactory`). Sin `lgpio`, el `Dispenser`
  degrada a stub con motivo claro.
- **Concurrencia.** El acceso a las estructuras de órdenes está protegido por
  `asyncio.Lock`; el dispensador usa un `threading.Lock` para evitar doble
  disparo. El dispensado corre en un hilo (`asyncio.to_thread`) para no bloquear
  el event loop durante el pulso de 2.5 s.
- **Comparación de montos en enteros.** Nunca se compara en `float`: el monto se
  maneja en unidades base (`int`) para evitar errores de redondeo.
- **Emparejamiento por monto (sin `reference`).** `app.py::_on_payment` intenta
  primero por `reference`; si la transacción no lo incluye (Phantom al digitar el
  monto), empareja por **monto con tolerancia `AMOUNT_TOLERANCE = 10_000`
  unidades base (±0.01 USDC)** contra la orden pendiente más antigua que coincida.
  Diferencias mínimas por ajustes de fees/límite de cómputo se aceptan.
- **Detección < 2 s.** El `PaymentMonitor` se construye con
  `reconcile_interval=2.0` (en `app.py::_init_backend`), lo que garantiza que un
  pago se detecta en ≤ ~2 s aunque el RPC público no entregue notificaciones
  WebSocket fiables.
- **Cancelación sin estado de carrera con el pago.** La cancelación se valida
  bajo `ORDERS_LOCK` y solo transiciona desde `pendiente`. Si el pago llegó
  primero (`_on_payment` ya marcó `pagada`), `cancel_order` devuelve 400. El
  `reference` se libera tanto al cancelar como al expirar (`ORDERS_BY_REFERENCE`),
  evitando asociar pagos tardíos.
- **Tema y estáticos.** La web usa `static/style.css` (tema dark premium, animación
  de compra `buyPulse`/`buyRing`/`buyWiggle`) y `static/coca-cola.png` (512×512
  RGBA, generada con Pillow). Los estáticos se montan con
  `app.mount("/static", StaticFiles(...))`.
- **Red por configuración.** La red la decide `config.yaml`/`.env`, no el código.
  Actualmente `mainnet-beta` con USDC real; el respaldo devnet está en
  `config.devnet.yaml.bak`.
