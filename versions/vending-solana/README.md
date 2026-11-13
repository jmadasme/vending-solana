# 🥤 Vending Solana — Máquina expendedora cripto (Coca-Cola · USDC)

Máquina expendedora que dispensa **latas de Coca-Cola** pagando con **USDC sobre
Solana** mediante el protocolo **Solana Pay**, con **confirmación instantánea**
(~400 ms de bloque, flujo completo < 2 s). El "cerebro" es una **Raspberry Pi 5**
sin pantalla táctil: **el teléfono del cliente es la pantalla**, y la máquina
obtiene internet de forma autónoma vía un **módem LTE Teltonika**.

Cuando el pago se confirma, la Pi dispara un **relé GPIO** (pulso de **2.5 s**)
que acciona un **motor** para dispensar la lata.

La interfaz web usa un **tema oscuro premium** estilo Solana/Phantom (acentos
violeta `#9945FF` y menta `#14F195`), con imagen de lata hiperrealista, animación
de compra en el botón y layout que ocupa todo el alto de pantalla.

> **⚠️ Red de trabajo: `mainnet-beta` (dinero REAL).** Los precios se expresan en
> unidades base (6 decimales): `1.00 USDC = 1_000_000`. El mint USDC es el real
> (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`). Antes de operar en mainnet,
> lee la sección [Advertencias y precauciones](#advertencias-y-precauciones). La
> configuración original de **devnet** se conserva en `config.devnet.yaml.bak`.

---

## Tabla de contenidos

1. [Qué es y cómo funciona](#qué-es-y-cómo-funciona)
2. [Principio de funcionamiento](#principio-de-funcionamiento)
3. [Forma de operar (flujo de compra)](#forma-de-operar-flujo-de-compra)
4. [Arquitectura](#arquitectura)
5. [Catálogo de productos](#catálogo-de-productos)
6. [Tabla de pines GPIO](#tabla-de-pines-gpio)
7. [Diagrama de conexión](#diagrama-de-conexión)
8. [API y ejemplos de uso](#api-y-ejemplos-de-uso)
9. [Visor de ventas (scripts/sales.py)](#visor-de-ventas-scriptssalespy)
10. [Tema visual](#tema-visual)
11. [Configuración](#configuración)
12. [Despliegue en la Raspberry Pi 5](#despliegue-en-la-raspberry-pi-5)
13. [Módem LTE Teltonika](#módem-lte-teltonika)
14. [Servicio systemd (arranque automático)](#servicio-systemd-arranque-automático)
15. [Advertencias y precauciones](#advertencias-y-precauciones)
16. [Estructura del repositorio](#estructura-del-repositorio)

---

## Qué es y cómo funciona

La máquina no tiene teclado ni pantalla física. Un **QR impreso** (o el acceso a
la red local) lleva al cliente a una **web app servida por la Pi** (FastAPI).
Desde su móvil el cliente ve el catálogo, elige la Coca-Cola y obtiene un
**QR Solana Pay** (Transfer Request). Escanea el QR con su wallet
(Phantom/Solflare), firma la transferencia de USDC, y la Pi **detecta el pago en
tiempo real** para dispensar el producto.

**Ventajas del enfoque:**

| Criterio | Valor |
|---|---|
| Confirmación | ~400 ms (un bloque de Solana), flujo < 2 s |
| Fee por venta | ~$0.0005 (solo fee de red, sin comisión porcentual) |
| Volatilidad | Ninguna (USDC anclado 1:1 al dólar) |
| Intermediarios | Ninguno (self-custodial, wallet a wallet) |
| Chargebacks | No (transacción final una vez confirmada) |

---

## Principio de funcionamiento

El núcleo es la **detección instantánea** del pago (< 2 s), sin esperar que el
cliente haga nada:

1. **Transfer Request (Solana Pay).** Para cada orden se genera una URL
   `solana:<pubkey_comercio>?spl-token=[REDACTED]
   (se omite `amount`, porque wallets como Phantom lo parsean mal — el monto se
   refleja en el QR con 6 decimales en la pantalla y el cliente lo digita al
   confirmar) y su QR correspondiente. El `reference` es una **pubkey aleatoria
   única por orden** (evita *replay* y doble dispensado).
2. **Detección por reconciliación periódica (~2 s).** El monitor consulta las
   firmas recientes de la **ATA de USDC del comercio** (`getSignaturesForAddress`)
   y re-inspecciona cada transacción nueva (`getTransaction`), con un
   **WebSocket `account_subscribe`** como complemento cuando el RPC lo soporta.
   En la práctica con el RPC público la reconciliación de ~2 s es el mecanismo
   principal (verificado en producción: detección < 2 s).
3. **Emparejamiento por monto con tolerancia.** Al detectar una transferencia se
   intenta primero emparejar por **`reference`** (si viene en el QR); si no viene,
   porque el cliente digitó el monto manualmente (Phantom no siempre lo incluye),
   se empareja por **monto dentro de ±0.01 USDC** con la orden pendiente más
   antigua que coincida (`AMOUNT_TOLERANCE = 10_000` unidades base). El monto se
   valida en unidades base (6 decimales); diferencias mínimas (< 0.01 USDC) por
   fees/límites de cómputo se aceptan.
4. **Dispensado.** Si el monto y (si aplica) el `reference` coinciden, la orden
   pasa de `pendiente` → `pagada` → `dispensada` y se dispara el relé GPIO (2.5 s).
5. **Cancelación.** Mientras la orden está `pendiente`, el cliente puede
   **cancelarla** desde la página de pago (`POST /order/{id}/cancel`); la orden
   pasa a `cancelada` y su `reference` se libera. Si ya está pagada, se rechaza.
6. **Resiliencia.** Reconexión del WebSocket con *backoff exponencial*,
   **reconciliación al reconectar** y **reconciliación periódica** (configurable)
   para recuperar pagos que confirmaron durante un corte de red, y **failover** a
   un RPC de respaldo si el primario falla.

**Ciclo de vida de una orden:**

```
pendiente ──(pago correcto)──▶ pagada ──(relé disparado)──▶ dispensada
    │                              │
    │                              └─(fallo al dispensar)──▶ pagada (reintentable)
    ├─(timeout 50 s)──▶ expirada
    ├─(cancelación del usuario)──▶ cancelada
    └─(monto distinto)──▶ monto_incorrecto (registrada, sin dispensar)
```

---

## Forma de operar (flujo de compra)

| Paso | Cliente | Máquina (Raspberry Pi) |
|---|---|---|
| 0 | Se acerca a la máquina | Sirve la web app en su pantalla |
| 1 | Ve el catálogo en la pantalla | — |
| 2 | Toca "Comprar" en la Coca-Cola | — |
| 3 | — | Crea la orden: `reference` único, `order_id`, producto, monto, estado `pendiente` |
| 4 | Ve el QR de pago USDC en pantalla | Devuelve el *Transfer Request* (URL + QR) |
| 5 | Su wallet abre la pantalla de pago; digita **1.00** si no está precargado | — |
| 6 | Aprueba (firma) la transacción | — |
| 7 | La tx se confirma (~400 ms); el USDC llega a la ATA del comercio | — |
| 8 | — | Detecta la transferencia por **reconciliación ~2 s** (o WebSocket) y empareja por monto (o `reference`) |
| 9 | — | Orden `pagada` → relé GPIO 2.5 s → motor dispensa la lata |
| 10 | Ve "Pago confirmado. ¡Gracias! Retira tu producto." | Vuelve al catálogo |
| — | **Opcional:** pulsa "Cancelar orden" antes de pagar | Orden `cancelada` (sin cobro); el `reference` se libera |

El navegador del cliente hace **polling ligero** (`/order/{id}` cada ~1.5 s)
mientras el backend escucha por WebSocket, para reflejar el cambio de estado
(pendiente → confirmado / expirada / cancelada) en la pantalla.

> **Cancelación:** el botón **"Cancelar orden"** está siempre visible en la
> página de pago. Cancela **directamente, sin confirmación**, y solo es válido
> mientras la orden está `pendiente`. Si el pago ya se confirmó, el backend
> rechaza la cancelación (HTTP 400) para no generar un dispensado sin cobro.

---

## Arquitectura

```
[Teléfono cliente] ──HTTP/QR──▶ [Raspberry Pi 5 — FastAPI (app.py)]
                                    ├─ wallet.py     (keypair comercio + ATA USDC)
                                    ├─ payments.py   (Transfer Request + account_subscribe + validación)
                                    ├─ dispenser.py  (relé/motor + LEDs GPIO)
                                    └─ catalog/config (YAML)
                                              │
                        Internet LTE Teltonika ── RPC mainnet-beta (HTTP + WebSocket)

[Terminal del operador] ──▶ scripts/sales.py (visor de ventas SOLO-LECTURA)
```

| Módulo | Responsabilidad |
|---|---|
| `app.py` | Web app FastAPI, ciclo de vida de la orden (incl. cancelación), orquestación del dispensado |
| `wallet.py` | Carga/crea el keypair del comercio y deriva/crea la ATA de USDC |
| `payments.py` | Transfer Request, `account_subscribe`, validación monto + `reference`, reconciliación y failover |
| `dispenser.py` | Control GPIO del relé/motor y LEDs (gpiozero + backend lgpio, Pi 5) |
| `config.py` | Carga de `config.yaml` + `.env` + variables de entorno |
| `catalog.yaml` | Productos (SKU, nombre, precio, pin, duración de pulso) |
| `scripts/sales.py` | Visor de ventas de consola **solo-lectura** (balance + transacciones) |

---

## Catálogo de productos

Definido en [`catalog.yaml`](catalog.yaml):

| SKU | Producto | Precio USDC | Precio (unidades base) | Duración pulso | Pin |
|---|---|---|---|---|---|
| `coca-cola` | Coca-Cola (lata 330 ml) | 1.00 | `1_000_000` | 2.5 s | GPIO 17 |

> El precio se expresa en **unidades base** con 6 decimales:
> `precio_USDC × 10^6`. Para añadir un producto basta con añadir una entrada en
> `catalog.yaml` (ver `docs/developer.md`).

---

## Tabla de pines GPIO

> **Importante (Pi 5):** la Raspberry Pi 5 **no tiene `/dev/gpiomem`**; usa
> `libgpiod` (`/dev/gpiochip4`). El control GPIO se hace con **gpiozero +
> backend `lgpio`** (nunca `RPi.GPIO`). Los pines elegidos no chocan con I2C
> (GPIO 2/3), SPI (GPIO 8/9/10/11) ni UART (GPIO 14/15).

| Pin BCM | Pin físico | Modo | Componente conectado | Notas |
|---------|-----------|------|---------------------|-------|
| GPIO 17 | 11 | OUTPUT | Relé (motor) | Activo-alto; **pulso de 2.5 s** por dispensa |
| GPIO 27 | 13 | OUTPUT | LED "listo" | Encendido mientras la máquina está operativa |
| GPIO 22 | 15 | OUTPUT | LED "pagado" | Encendido durante/tras el dispensado exitoso |
| GPIO 23 | 16 | OUTPUT | LED "error" | Encendido ante un fallo (opcional) |
| GND | 6, 9, 14, 20, 25, 30, 34, 39 | — | Tierra común | Referencia para el relé y los LEDs |
| 5V | 2, 4 | Alimentación | (solo referencia) | **No** alimentar el motor desde la Pi |
| 3.3V | 1, 17 | Alimentación | (solo referencia) | Para lógica, no para cargas |

**Reglas de conexión:**

- El relé **no** se conecta directo al GPIO: el pin solo manda la señal de
  control (activo-alto) a través de un **transistor/driver** (o un módulo relé
  con optoacoplador). La bobina del relé se alimenta de una fuente externa.
- El motor va por su **propia fuente** de alimentación (nunca de los 5 V de la
  Pi), con un driver tipo **L298N o MOSFET**.
- Cada LED lleva su **resistencia limitadora (~330 Ω)** en serie.

---

## Diagrama de conexión

```
                          FUENTE DEL MOTOR (externa)
                          (p. ej. 5–12 V según motor)
                                   │
                                   │ (+)
                                   ▼
                          ┌─────────────────┐
                          │  DRIVER MOTOR   │        ┌──────────┐
                          │  (L298N/MOSFET) │◀──────▶│  MOTOR   │
                          └────────┬────────┘        └──────────┘
                                   │ IN (señal de control)
                                   │
     Raspberry Pi 5 (BCM)          │
  ┌────────────────────────┐        │
  │                        │        │
  │  GPIO 17 ──(R 1k)──▶ ┌──▼─────────┐
  │                      │  MÓDULO RELÉ │── COM ──▶ a la fase del motor
  │                      │  (1 canal)   │── NO/NC ─▶ interrumpe/cierra el circuito
  │                      └──▲─────────┘
  │                         │ VCC (fuente del relé)
  │                         │ GND
  │                        │
  │  GPIO 27 ──(330 Ω)──▶ LED "listo"  ── GND
  │  GPIO 22 ──(330 Ω)──▶ LED "pagado" ── GND
  │  GPIO 23 ──(330 Ω)──▶ LED "error"  ── GND
  │                        │
  │  GND ──────────────────┴──▶ tierra común (relé + LEDs + driver)
  │                        │
  │  USB / LAN ◀────▶ Módem LTE Teltonika (TRM240/250) + SIM
  └────────────────────────┘
```

**Descripción textual del conexionado:**

1. **Relé + motor.** Conecta la señal de `GPIO 17` a la entrada de control del
   módulo relé (a través de una resistencia de ~1 kΩ y, si el módulo no trae
   optoacoplador, un transistor NPN como el 2N2222 o un driver). El relé
   conmuta la fase del motor: `COM` a la fuente del motor, `NO` (normalmente
   abierto) a una de las terminales del motor, y la otra terminal del motor a la
   masa de su fuente. La bobina del relé y el motor se alimentan de su **propia
   fuente**, compartiendo **solo la tierra (GND)** con la Pi.
2. **LEDs.** Ánodo de cada LED a su pin (`GPIO 27/22/23`), cátodo a una
   resistencia de ~330 Ω y de ahí a `GND`.
3. **Fuente.** Alimenta la Pi con su fuente oficial de 5 V/5 A (USB-C). El motor
   y el relé usan fuentes separadas dimensionadas para su corriente.
4. **Conectividad.** El módem LTE Teltonika (TRM240/TRM250) con su SIM se conecta
   por USB a la Pi y queda expuesto como `/dev/ttyUSB0..4` (gestión por
   `wvdial`/`ppp`/`usb-modeswitch`).

---

## API y ejemplos de uso

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Catálogo (HTML) |
| POST | `/order` | Crea una orden; devuelve Transfer Request + QR (base64) |
| GET | `/order/{order_id}` | Estado de la orden (JSON, para polling) |
| GET | `/order/{order_id}/qr` | Imagen PNG del QR de pago USDC |
| GET | `/order/{order_id}/payment` | Página HTML de pago (QR + estado + botón cancelar) |
| POST | `/order/{order_id}/cancel` | Cancela una orden pendiente (rechazada si ya se pagó) |
| GET | `/order/{order_id}/cancelled` | Página HTML "orden cancelada" |
| GET | `/order/{order_id}/confirmed` | Página "pago confirmado" |
| GET | `/order/{order_id}/expired` | Página "orden expirada" |
| GET | `/healthz` | Salud: RPC + GPIO + wallet + monitor |
| POST | `/api/test/dispense` | Dispensado manual (requiere `X-Test-Token`) |

### Crear una orden

```bash
curl -s -X POST http://localhost:8000/order \
  -H 'Content-Type: application/json' \
  -d '{"sku":"coca-cola"}'
```

Respuesta (resumen):

```json
{
  "order_id": "a1b2c3d4e5f6",
  "sku": "coca-cola",
  "product_name": "Coca-Cola (lata 330 ml)",
  "amount_base": 1000000,
  "price_usdc": 1.0,
  "reference": "8f2…(pubkey aleatoria)",
  "transfer_request": "solana:<pubkey>?spl-token=[REDACTED]",
  "qr_base64": "iVBORw0KGgo…",
  "timeout_s": 50
}
```

### Consultar el estado de una orden

```bash
curl -s http://localhost:8000/order/a1b2c3d4e5f6
```

```json
{
  "order_id": "a1b2c3d4e5f6",
  "status": "pendiente",
  "product_name": "Coca-Cola (lata 330 ml)",
  "amount_base": 1000000,
  "price_usdc": 1.0,
  "reference": "8f2…",
  "signature": null,
  "paid_amount": null,
  "remaining_s": 42
}
```

### Cancelar una orden

```bash
curl -s -X POST http://localhost:8000/order/a1b2c3d4e5f6/cancel
```

```json
{ "order_id": "a1b2c3d4e5f6", "status": "cancelada" }
```

Si la orden ya está `pagada` (o `dispensada`), el backend responde `400`:

```json
{ "detail": "No se puede cancelar: la orden está pagada" }
```

### Obtener el QR como imagen

```bash
curl -s -o qr.png http://localhost:8000/order/a1b2c3d4e5f6/qr
```

### Chequeo de salud

```bash
curl -s http://localhost:8000/healthz
```

```json
{
  "status": "ok",
  "network": "mainnet-beta",
  "rpc_ok": true,
  "rpc_http": "https://api.mainnet-beta.solana.com",
  "wallet": "<pubkey_comercio>",
  "merchant_ata": "<ata_usdc>",
  "usdc_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  "gpio": {
    "available": true,
    "mode": "real",
    "reason": "",
    "dispense_pin": 17,
    "pulse_seconds": 2.5
  },
  "monitor": "running",
  "pending_orders": 0
}
```

### Ejemplo de Transfer Request Solana Pay

```
solana:<pubkey_comercio>?spl-token=[REDACTED]
```

> Nota: el `amount` se omite a propósito (ver *Principio de funcionamiento*): el
> monto se muestra en la pantalla y el cliente lo digita en su wallet al
> confirmar. El `reference` es imprescindible para el emparejamiento por QR.

---

## Visor de ventas (scripts/sales.py)

`scripts/sales.py` es un **visor de ventas de consola SOLO-LECTURA**: consulta la
blockchain (balance + transacciones de la ATA del comercio) y muestra las ventas.
**No abre puertos, no escucha nada, no usa claves privadas** y no modifica nada:
es seguro ejecutarlo en tu terminal mientras la máquina está funcionando.

Se ejecuta siempre con el entorno virtual:

```bash
.venv/bin/python scripts/sales.py              # balance + últimas 15 transacciones
.venv/bin/python scripts/sales.py --watch      # vigilancia en vivo (actualiza cada 5 s)
.venv/bin/python scripts/sales.py --last 20    # muestra las últimas 20 transacciones
```

Salida de ejemplo:

```
  🌐 Red: mainnet-beta  |  RPC: https://api.mainnet-beta.solana.com
  📦 Visor SOLO-LECTURA (no abre puertos, no usa claves privadas)

  💰 Balance de la ATA del comerciante:
      3.00 USDC
  ATA: <ata_usdc>

  📜 Últimas 15 transacciones:

  🥤  VENTAS DETECTADAS: 2
  ------------------------------------------------------------
  ✅ 2026-09-12 11:52:10 UTC  1.00 USDC
      ref:8f2…  sig:5kQ2…
  ✅ 2026-09-12 11:51:26 UTC  1.00 USDC
      ref:9c1…  sig:3mP8…
  ------------------------------------------------------------
```

> El visor deriva la ATA del comercio a partir de `wallet.json` **sin exponer la
> clave privada** (solo usa la dirección). Si no existe `wallet.json`, te pide la
> dirección del comercio de forma interactiva.

### Reembolso al cliente (scripts/refund.py)

`scripts/refund.py` devuelve USDC desde la ATA del comercio (Raspberry Pi) hacia
una wallet destino — útil para pruebas, devoluciones o reposición de saldo:

```bash
.venv/bin/python scripts/refund.py \
  --to <PUBKEY_DESTINO> --amount 1.00 --yes
```

- Es una transferencia SPL `transfer_checked` normal (sin `reference`: no es una
  venta).
- Si la ATA USDC del destino no existe, se crea automáticamente (el rent lo paga
  la wallet del comercio).
- La wallet del comercio necesita SOL para la comisión (~0.000005 SOL).
- Nunca imprime la clave privada.

---

## Tema visual

La interfaz usa un **tema oscuro premium**: pantalla oscura con degradados,
acentos violeta `#9945FF` y menta `#14F195`. **No se menciona la red** en la UI:
solo se promueve la moneda **USDC**.

- **Paleta:** fondo negro profundo con degradados radiales, acentos violeta
  `#9945FF` y menta `#14F195`.
- **Layout:** ocupa el **100% del alto** de pantalla (`100dvh`), con tarjetas
  flexibles (`grid`), `footer` fijo al pie y `glassmorphism` (paneles
  translúcidos con `backdrop-filter`).
- **Animación de compra:** al tocar "Comprar", el botón ejecuta una animación
  triple (`buyPulse` / `buyRing` / `buyWiggle`) mientras se crea la orden.
- **Pantalla de confirmación:** celebración con emojis 🎉🥤, confeti animado y el
  mensaje "¡GRACIAS!" con gradiente menta→violeta.
- **Imagen de producto:** `static/coca-cola.png` es una lata hiperrealista en
  **512×512 RGBA con fondo transparente**, generada con **Pillow**.
- **Botón de cancelar:** estilizado como `btn-danger` (rojo sutil con borde),
  coherente con el tema.

Todo el estilo vive en [`static/style.css`](static/style.css). Las variables de
color se definen en `:root` y son fáciles de re-tematizar.

---

## Configuración

Las fuentes de configuración, en orden de prioridad, son:
**variables de entorno > `.env` > `config.yaml`** (los valores por defecto en el
código quedan al final de la cadena).

Parámetros principales (valores en `config.yaml`):

| Parámetro | Valor | Descripción |
|---|---|---|
| `network` | `mainnet-beta` | Red de trabajo (**mainnet**, dinero real) |
| `rpc_http` | `https://api.mainnet-beta.solana.com` | RPC HTTP primario |
| `rpc_ws` | `wss://api.mainnet-beta.solana.com` | RPC WebSocket |
| `rpc_http_backup` | `https://api.mainnet-beta.solana.com` | RPC de respaldo (failover) |
| `usdc_mint` | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | Mint **USDC real de mainnet** |
| `usdc_decimals` | `6` | Decimales del token |
| `wallet_path` | `wallet.json` | Ruta del keypair del comercio (fuera de git) |
| `order_timeout_seconds` | `50` | Timeout de expiración de orden |
| `dispense_pulse_seconds` | `2.5` | Duración del pulso del relé |
| `dispense_pin` | `17` | Pin del relé |
| `led_ready_pin` / `led_paid_pin` / `led_error_pin` | `27` / `22` / `23` | LEDs de estado |
| `dispenser_mode` | `auto` | `auto` (GPIO real si existe) o `stub` (sin GPIO) |

Variables de entorno (ver `.env.example`): `WALLET_PATH`, `RPC_HTTP`, `RPC_WS`,
`RPC_HTTP_BACKUP`, `USDC_MINT`, `USDC_DECIMALS`, `ORDER_TIMEOUT_SECONDS`,
`DISPENSE_PULSE_SECONDS`, `DISPENSE_PIN`, `TEST_TOKEN`, `DISPENSER_MODE`.

> **Red actual y respaldo devnet:**
> - `config.yaml` apunta a **mainnet-beta** con el **USDC real**.
> - En la Raspberry Pi, el `.env` **sobrescribe** `config.yaml` y también apunta
>   a **mainnet**.
> - La configuración original de **devnet** (mint de prueba
>   `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`) se conserva en
>   `config.devnet.yaml.bak`. Para volver a devnet, renómbralo a `config.yaml`.

---

## Despliegue en la Raspberry Pi 5

### 1. Prerrequisitos de sistema

```bash
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip \
  libatlas-base-dev libopenblas-dev wvdial ppp usb-modeswitch
# Backend lgpio de gpiozero, imprescindible en Pi 5 (libgpiod /dev/gpiochip4)
sudo apt-get install -y python3-lgpio
```

### 2. Clonar y preparar el entorno

```bash
git clone <URL_DEL_REPOSITORIO> vending-solana
cd vending-solana

# Alternativa rápida: el script hace todo (deps de sistema + venv + pip + .env)
bash setup_pi.sh

# O manualmente:
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configurar variables (mainnet)

```bash
cp .env.example .env
# Editar .env con el RPC de MAINNET y la ruta del keypair
nano .env
```

Contenido mínimo de `.env` (mainnet):

```
WALLET_PATH=wallet.json
RPC_HTTP=https://api.mainnet-beta.solana.com
RPC_WS=wss://api.mainnet-beta.solana.com
RPC_HTTP_BACKUP=https://api.mainnet-beta.solana.com
```

> `USDC_MINT` no es necesario si `config.yaml` ya apunta al USDC real
> (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`). Recuerda: **mainnet = dinero
> real**.

### 4. Generar la wallet del comercio (keypair nuevo)

El keypair se crea automáticamente en el primer arranque, o explícitamente:

```bash
source .venv/bin/activate
python scripts/setup_wallet.py
```

> **En mainnet no hay airdrop de SOL.** Debes fondear la wallet con **SOL real**
> para pagar el rent de la ATA (≈0.002 SOL) y con **USDC real** si haces pruebas
> de venta. Mantén el keypair (`wallet.json`) **fuera de git** y con permisos
> `0600`.

### 5. Fondear la ATA (mainnet)

En mainnet, la ATA de USDC se crea automáticamente al recibir el primer envío de
USDC (o al arrancar, si la wallet tiene SOL para el rent). Asegúrate de:

- Tener **SOL real** en la wallet para el rent de la ATA.
- Enviar USDC real a la ATA del comercio solo para pruebas (el saldo de la ATA
  es lo que el visor `sales.py` muestra como "Balance").

### 6. Ejecutar la web app

```bash
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

La app escucha en `0.0.0.0:8000` para ser accesible desde la red local del
cliente.

### 7. Verificar el despliegue

```bash
curl -s http://<IP_DE_LA_PI>:8000/healthz
```

Debe responder `200` con `rpc_ok: true`, `network: mainnet-beta`,
`gpio.available: true` (`mode: real`, chip `gpiochip4`) y la wallet cargada.

### 8. Prueba del dispensador (LED de prueba)

Antes de conectar el motor, valida el pulso con un LED + resistencia en GPIO 17:

```bash
source .venv/bin/activate
python scripts/dispense_test.py --pin 17 --seconds 2.5
# o en una línea:
python -c "from dispenser import dispense; dispense(17, 2.5)"
```

El LED debe encenderse **2.5 s** y apagarse.

### 9. Desplegar cambios a la Pi (scp + systemd)

Para publicar un archivo actualizado en la Pi y reiniciar el servicio:

```bash
# 1. Copiar el archivo a la Pi (usa tu contraseña SSH; no la versiones)
sshpass -p '<CONTRASEÑA_SSH>' scp -o StrictHostKeyChecking=accept-new \
  <archivo> jma@192.168.194.136:~/vending-solana/<ruta>

# 2. Reiniciar el servicio vending en la Pi
ssh jma@192.168.194.136 'sudo systemctl restart vending'
```

> **Nunca sobrescribas `.env` ni `wallet.json`** en la Pi: contienen el keypair de
> producción y la configuración mainnet. Despliega solo código, plantillas,
> `static/` y `config.yaml` cuando corresponda.

---

## Módem LTE Teltonika

La Pi obtiene internet autónomo a través del módem **Teltonika TRM240/TRM250** +
SIM. Pasos de configuración:

1. Inserta la SIM y conecta el módem por USB a la Pi.
2. Verifica que aparezca como puerto serie (`lsusb` y `ls /dev/ttyUSB*`); si no,
   asegúrate de que `usb-modeswitch` está instalado (ya lo instala `setup_pi.sh`).
3. Configura la conexión PPP con `wvdial` (archivo `/etc/wvdial.conf` con el
   APN de tu operador) y levántala con `wvdial`.
4. Verifica salida a internet desde la Pi:

```bash
curl -sI https://api.mainnet-beta.solana.com | head -n 1   # HTTP/1.1 200 OK
```

> La máquina **no necesita** internet perfecto: el WebSocket se reconecta con
> backoff exponencial y, al volver, la **reconciliación** recupera los pagos
> confirmados durante el corte (nunca se pierde un pago confirmado).

---

## Servicio systemd (arranque automático)

Crea `/etc/systemd/system/vending.service`:

```ini
[Unit]
Description=Vending Solana (Coca-Cola USDC)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/vending-solana
ExecStart=/home/pi/vending-solana/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
EnvironmentFile=/home/pi/vending-solana/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Luego habilítalo y arráncalo:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vending
sudo systemctl status vending        # comprobar estado
journalctl -u vending -f             # ver logs en vivo
```

> Ajusta `User`, `WorkingDirectory`, `ExecStart` y `EnvironmentFile` a las rutas
> reales de la Pi. El reinicio tras desplegar cambios se hace con
> `sudo systemctl restart vending`.

---

## Advertencias y precauciones

- **🔑 Nunca subas la clave privada.** `wallet.json`, `.env`, `*.key`,
  `id.json` y `*.keypair.json` están excluidos en `.gitignore`. **Jamás**
  incluyas el keypair real en el repositorio ni lo pegues en issues/logs. Usa
  placeholders como `<TU_KEYPAIR>` en la documentación.
- **💸 Mainnet = dinero REAL.** Este proyecto está configurado para
  **mainnet-beta** con el **USDC real**
  (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`). Cualquier venta mueve fondos
  reales del cliente al keypair del comercio; una mala configuración (mint, RPC,
  precio) puede causar **pérdidas reales e irreversibles**. Prueba primero en
  devnet (`config.devnet.yaml.bak` con el mint de Circle faucet) antes de tocar
  mainnet, y mantén la wallet de producción con fondos acotados.
- **⚡ Voltajes del motor.** El motor y el relé se alimentan de **fuentes
  separadas** de los 5 V de la Pi. Nunca alimentes el motor desde los pines de la
  Pi (riesgo de quemar la placa). Comparte solo la tierra (GND).
- **📡 RPC público mainnet.** El RPC público de mainnet puede tener límites de
  *rate*. Configura un RPC de respaldo (`rpc_http_backup`) y, en producción,
  considera un proveedor con SLA (p. ej. Helius).
- **🛑 Cancelación y carrera con el pago.** La cancelación solo se acepta si la
  orden sigue `pendiente`. Si el pago llega en el mismo instante, el backend
  prioriza la transacción ya confirmada y **rechaza** la cancelación (HTTP 400)
  para no dispensar sin cobro. No hay reembolso automático de cancelaciones
  tardías.
- **📦 Dependencia `spl`.** El namespace `spl.token` viene **empaquetado dentro
  de `solana>=0.40`**. No instales paquetes externos `spl-*` (el paquete
  `spl-token-py` es malicioso y exfiltra claves).
- **⏱️ scp/despliegue.** Al sincronizar con la Pi, **excluye** `.env` y
  `wallet.json` para no sobrescribir el keypair de producción. Nunca incluyas la
  contraseña SSH en scripts ni documentación.
- **🛑 Endpoint de prueba.** `POST /api/test/dispense` dispensa manualmente; está
  protegido por `X-Test-Token` (config `TEST_TOKEN`). Déjalo vacío para
  deshabilitarlo en producción.
- **🔌 Carga eléctrica de los LEDs.** Usa siempre resistencia limitadora (~330 Ω)
  en serie con cada LED para no dañar los pines GPIO.

---

## Estructura del repositorio

```
vending-solana/
├── app.py               # Web app FastAPI (punto de entrada)
├── wallet.py            # Keypair del comercio + ATA de USDC
├── payments.py          # Solana Pay + WebSocket/reconciliación + validación monto
├── dispenser.py         # Control GPIO (relé/motor + LEDs)
├── config.py            # Carga de configuración
├── catalog.yaml         # Productos (Coca-Cola)
├── config.yaml          # RPC, red (mainnet), mint, pines, timeouts
├── config.devnet.yaml.bak   # Respaldo de la configuración devnet original
├── requirements.txt     # Dependencias Python
├── setup_pi.sh          # Preparación del entorno en la Pi
├── .env.example         # Plantilla de variables sensibles
├── .gitignore           # Exclusiones (wallet, .env, etc.)
├── templates/           # index.html, payment.html, confirmed.html,
│                        #   cancelled.html, error.html
├── static/              # style.css (tema dark), coca-cola.png (512×512 RGBA)
├── scripts/
│   ├── setup_wallet.py      # Genera keypair + ATA (+ airdrop en devnet)
│   ├── dispense_test.py     # Prueba manual del pulso GPIO
│   ├── devnet_e2e_test.py   # Prueba end-to-end autónoma en devnet
│   ├── sales.py             # Visor de ventas SOLO-LECTURA (balance + tx)
│   └── refund.py            # Devuelve USDC desde la ATA del comercio (--to --amount)
└── docs/
    ├── datasheet.md     # Documentación de usuario
    └── developer.md     # Documentación técnica del código
```

---

## Documentación adicional

- [`docs/datasheet.md`](docs/datasheet.md) — Manual de usuario, especificaciones
  técnicas, solución de problemas y mantenimiento.
- [`docs/developer.md`](docs/developer.md) — Explicación del código fuente,
  arquitectura de módulos, flujo lógico y guía de extensión.

---

## Licencia

Proyecto educativo/demostrativo de integración Solana Pay sobre Raspberry Pi.
Consulta los términos de los componentes de terceros (Solana, USDC/Circle,
FastAPI, gpiozero) para su uso en producción.
