# Datasheet — Vending Solana (Máquina expendedora cripto)

> Manual de usuario, especificaciones técnicas, solución de problemas y
> mantenimiento de la máquina expendedora de Coca-Cola con pago en USDC/Solana.

---

## 1. Descripción general

La **Vending Solana** es una máquina expendedora de latas de Coca-Cola cuyo pago
se realiza en **USDC sobre Solana** mediante el protocolo **Solana Pay**. El
cerebro es una **Raspberry Pi 5** que sirve una web app al teléfono del cliente
(el teléfono actúa como pantalla). Al confirmarse el pago (≈400 ms), la Pi
dispara un **relé** (pulso de 2.5 s) que acciona un **motor** para dispensar la
lata. Internet autónomo vía **módem LTE Teltonika**.

La interfaz web usa un **tema oscuro premium** estilo Solana/Phantom (violeta
`#9945FF` y menta `#14F195`), con imagen de lata hiperrealista y animación de
compra en el botón.

> **⚠️ Red de trabajo: `mainnet-beta` (dinero REAL).** El token es **USDC real**
> (mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`). Cada venta mueve fondos
> reales. La configuración de **devnet** original está respaldada en
> `config.devnet.yaml.bak`.

| Dato | Valor |
|---|---|
| Producto | Coca-Cola (lata 330 ml) |
| Precio | 1.50 USDC (`1_500_000` unidades base) |
| Red | Solana **mainnet-beta** (dinero real) |
| Token | USDC real (mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) |
| Confirmación | ~400 ms (bloque), flujo completo < 2 s |
| Dispensado | Relé + motor, pulso 2.5 s |

---

## 2. Cómo operar la máquina (cliente)

1. Escanea el **QR impreso** en la máquina (o únete a su red local) para abrir la
   web app en tu móvil.
2. Verás el catálogo con la **Coca-Cola (lata 330 ml) — 1.50 USDC**.
3. Toca **"Comprar"** (el botón muestra una animación mientras se crea la orden).
4. Aparecerá un **QR Solana Pay** (y/o el botón de tu wallet). Escanéalo o tócalo.
5. Tu wallet (Phantom/Solflare) abrirá "Pagar 1.50 USDC a Vending Coca-Cola".
6. **Aprueba** la transacción.
7. La pantalla mostrará **"Pago confirmado. Retira tu producto."**.
8. Recoge tu lata del dispensador.

### Cancelar una compra

- Mientras no hayas pagado, pulsa **"Cancelar orden"** en la página de pago. La
  orden se cancela **directamente (sin confirmación)** y verás
  **"Orden cancelada"**. No se realiza ningún cobro.
- Si el pago **ya se confirmó**, la cancelación **no es posible**: el backend la
  rechaza y se entrega el producto. No hay reembolso automático.

> Si no pagas dentro del tiempo límite (5 minutos), la orden **expira** y debes
> volver a empezar.

---

## 3. Cómo operar la máquina (operador)

### Puesta en marcha

```bash
sudo systemctl start vending     # arranca la web app (si usas systemd)
# o manualmente:
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Verifica la salud:

```bash
curl -s http://localhost:8000/healthz
```

Espera `status: ok`, `rpc_ok: true`, `network: mainnet-beta` y
`gpio.available: true` (`mode: real`).

### Consultar las ventas (visor solo-lectura)

`scripts/sales.py` muestra el **balance de la ATA** y las **últimas ventas** sin
tocar puertos ni claves privadas:

```bash
.venv/bin/python scripts/sales.py          # balance + últimas 15 transacciones
.venv/bin/python scripts/sales.py --watch  # vigilancia en vivo (cada 5 s)
.venv/bin/python scripts/sales.py --last 20
```

### Reposición y estado de la máquina

- **Reabastecimiento de latas:** repón el cargador del motor manualmente (no hay
  sensor de stock en esta versión; consulta `healthz` solo para salud del
  servicio).
- **Fondos USDC:** la wallet del comercio acumula USDC en su ATA. Puedes
  retirarlos con cualquier wallet SPL usando la keypair del comercio
  (`wallet.json`) de forma segura y fuera de la máquina.
- **Logs:** `journalctl -u vending -f` (systemd) o la salida de `uvicorn`.

### Prueba del dispensador sin pago

```bash
# Con LED de prueba conectado al pin 17:
python scripts/dispense_test.py --pin 17 --seconds 2.5

# O vía HTTP (requiere TEST_TOKEN configurado):
curl -s -X POST http://localhost:8000/api/test/dispense \
  -H 'Content-Type: application/json' \
  -H 'X-Test-Token: [REDACTED]' \
  -d '{"pin":17,"seconds":2.5}'
```

### Desplegar cambios

```bash
# 1. Copiar el archivo a la Pi (usa tu contraseña SSH; no la versiones)
sshpass -p '<CONTRASEÑA_SSH>' scp -o StrictHostKeyChecking=accept-new \
  <archivo> jma@192.168.194.136:~/vending-solana/<ruta>

# 2. Reiniciar el servicio vending en la Pi
ssh jma@192.168.194.136 'sudo systemctl restart vending'
```

> No sobrescribas `.env` ni `wallet.json` en la Pi (contienen el keypair de
> producción y la config mainnet).

---

## 4. Especificaciones técnicas

### 4.1 Plataforma

| Componente | Especificación |
|---|---|
| Placa | Raspberry Pi 5 Model B (aarch64, Python 3.11) |
| GPIO | libgpiod (`/dev/gpiochip4`); **sin `/dev/gpiomem`** → gpiozero + backend lgpio |
| Red | Ethernet/WiFi (local) + módem LTE Teltonika (internet) |
| OS | Raspberry Pi OS (Debian/Ubuntu compatible) |

### 4.2 GPIO (mapeo de pines)

| Pin BCM | Pin físico | Modo | Componente | Notas |
|---------|-----------|------|------------|-------|
| GPIO 17 | 11 | OUTPUT | Relé (motor) | Activo-alto; pulso 2.5 s |
| GPIO 27 | 13 | OUTPUT | LED "listo" | Operativo |
| GPIO 22 | 15 | OUTPUT | LED "pagado" | Dispensado en curso/exitoso |
| GPIO 23 | 16 | OUTPUT | LED "error" | Fallo |
| GND | 6, 9, … | — | Tierra común | Referencia relé + LEDs |

### 4.3 Actuador

| Componente | Detalle |
|---|---|
| Relé | 1 canal, activo-alto, con transistor/driver u optoacoplador |
| Motor | Con driver L298N o MOSFET; alimentación propia |
| Pulso | 2.5 s por dispensa (configurable en `config.yaml` / por producto) |

### 4.4 Conectividad (Teltonika)

| Componente | Detalle |
|---|---|
| Módem | Teltonika TRM240/TRM250 + SIM |
| Interfaz | USB → `/dev/ttyUSB0..ttyUSB4` |
| Gestión | `wvdial` / `ppp` / `usb-modeswitch` |

### 4.5 Alimentación

- Raspberry Pi 5: fuente oficial 5 V / 5 A (USB-C).
- Motor y relé: **fuente separada** dimensionada para su corriente (comparten
  solo GND con la Pi).

### 4.6 Red y token (mainnet)

| Parámetro | Valor |
|---|---|
| `network` | `mainnet-beta` |
| `rpc_http` | `https://api.mainnet-beta.solana.com` |
| `rpc_ws` | `wss://api.mainnet-beta.solana.com` |
| `rpc_http_backup` | `https://api.mainnet-beta.solana.com` |
| `usdc_mint` | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (USDC real) |
| `usdc_decimals` | `6` |
| Devnet (respaldo) | `config.devnet.yaml.bak` (mint Circle `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`) |

### 4.7 Dependencias

**Sistema (apt):** `python3-venv`, `python3-pip`, `libatlas-base-dev`,
`libopenblas-dev`, `wvdial`, `ppp`, `usb-modeswitch`, `python3-lgpio`.

**Python (`requirements.txt`):**

| Paquete | Versión | Propósito |
|---|---|---|
| fastapi | >=0.110 | Web app / API |
| uvicorn[standard] | >=0.29 | Servidor ASGI |
| jinja2 | >=3.1 | Plantillas HTML |
| solders | >=0.21 | Keypairs/pubkeys |
| solana | >=0.40 | RPC + WebSocket (incluye namespace `spl`) |
| qrcode | >=7.4 | QR Solana Pay |
| pillow | >=10.0 | Render de imágenes (incl. `coca-cola.png`) |
| pydantic | >=2.0 | Validación |
| base58 | >=2.1 | Encoding pubkeys |
| python-dotenv | >=1.0 | Cargar `.env` |
| httpx | >=0.27 | Cliente HTTP |
| websockets | >=10.4 | Cliente WebSocket |
| gpiozero | >=2.0 | Control GPIO |
| lgpio | >=0.2.2.0 | Backend libgpiod (Pi 5) |
| PyYAML | >=6.0 | Leer YAML |

**Instalación:**

```bash
bash setup_pi.sh
# o manual:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y python3-lgpio
```

---

## 5. Solución de problemas comunes

### 5.1 Pago no detectado (la orden queda "pendiente")

| Síntoma | Causa probable | Solución |
|---|---|---|
| El cliente pagó pero la orden no avanza | Falta de conexión WebSocket / RPC | Verifica `healthz` → `rpc_ok: true`. Revisa internet LTE (`curl -sI https://api.mainnet-beta.solana.com`) |
| Pago llegó a otra cuenta/mint | El cliente pagó al mint/recipiente incorrecto | Confirma que el QR apunta a `merchant_ata` y mint correctos (en `healthz`). En mainnet el mint es `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` |
| Monto distinto al esperado | Cliente pagó cantidad incorrecta | La orden pasa a `monto_incorrecto`; devuelve manualmente |
| Orden expiró | El cliente tardó > 5 min | Pide al cliente rehacer la compra |

**Diagnóstico:** revisa los logs (`journalctl -u vending -f`). Busca líneas
`on_payment`, `Pago detectado sig=...`, `Pago sin orden pendiente asociada`,
`Monto incorrecto`.

### 5.2 Sin internet (módem LTE)

| Síntoma | Solución |
|---|---|
| `healthz` muestra `rpc_ok: false` | Verifica el módem: `lsusb`, `ls /dev/ttyUSB*`, `wvdial`. Comprueba el APN en `/etc/wvdial.conf`. |
| Cortes intermitentes | El WebSocket se reconecta solo (backoff). La reconciliación recupera pagos caídos al volver. |

### 5.3 El motor no gira

| Síntoma | Causa probable | Solución |
|---|---|---|
| Pago confirmado pero no dispensa | Relé/GPIO no operativo | Revisa `healthz` → `gpio.available: true` (`mode: real`). Si es `stub`, falta `lgpio` o GPIO. |
| LED de prueba no enciende | Cableado o pin incorrecto | Prueba `python scripts/dispense_test.py --pin 17 --seconds 2.5` con LED + resistencia |
| El relé "clic" pero el motor no gira | Alimentación del motor ausente | Verifica la fuente del motor y el cableado del relé (COM/NO) |
| `lgpio no instalado` en logs | Falta backend en Pi 5 | `sudo apt-get install -y python3-lgpio` (o `pip install lgpio`) |

### 5.4 Cancelación

| Síntoma | Causa probable | Solución |
|---|---|---|
| El botón "Cancelar orden" no cancela | La orden ya se pagó (o expiró) | Es correcto: solo se cancela en `pendiente`. Si se pagó, el producto ya se entrega. |
| Error "No se puede cancelar: la orden está pagada" | Carrera pago/cancelación | El pago se confirmó antes; no hay reembolso automático. |

### 5.5 La app no arranca

| Síntoma | Solución |
|---|---|
| `ImportError: spl.token` | Reinstala `solana>=0.40` (incluye el namespace `spl`). **No** instales `spl-token-py` (malicioso). |
| `ModuleNotFoundError: gpiozero` | `pip install -r requirements.txt` |
| Puerto 8000 ocupado | Cambia el puerto (`--port 8001`) o libera el puerto |
| `network` muestra `devnet` pero esperabas mainnet | Revisa que `config.yaml` (o el `.env` de la Pi) apunte a `mainnet-beta` y al mint real. Usa `config.devnet.yaml.bak` solo para pruebas. |

---

## 6. Mantenimiento

- **Semanal:** verifica `healthz` (RPC, GPIO, monitor) y los logs en busca de
  `monto_incorrecto` o `Pago sin orden pendiente` (para devoluciones manuales).
  Usa `scripts/sales.py` para conciliar el balance de la ATA con las ventas.
- **Mensual:** inspecciona cableado del relé/motor y las conexiones de los LEDs;
  confirma que las fuentes de alimentación no se calientan en exceso.
- **Trimestral:** revisa el consumo de disco/logs (`journalctl --vacuum-size`),
  actualiza dependencias (`pip install -U -r requirements.txt`) tras probar en
  devnet, y valida el correcto funcionamiento del módem LTE.
- **Seguridad:** mantén `wallet.json` con permisos `0600` y **fuera de git**.
  Nunca compartas la keypair. Rota el keypair si hay sospecha de exposición.
  **Recuerda que en mainnet la wallet guarda fondos reales.**

### Estados de orden (referencia)

| Estado | Significado |
|---|---|
| `pendiente` | Creada, esperando pago |
| `pagada` | Pago validado, dispensando/dispensado |
| `dispensada` | Producto entregado |
| `cancelada` | Cancelada por el usuario antes de pagar (reference liberado) |
| `expirada` | Timeout sin pago (reference descartado) |
| `monto_incorrecto` | Pago recibido con monto distinto (sin dispensar) |

---

## 7. Compatibilidad con otros modelos de Raspberry Pi

| Modelo | GPIO | Notas |
|---|---|---|
| Raspberry Pi 5 | ✅ Compatible (objetivo) | libgpiod `/dev/gpiochip4`; requiere `lgpio` |
| Raspberry Pi 4 B | ✅ Compatible | gpiozero backend lgpio también funciona |
| Raspberry Pi 3 B/B+ | ✅ Compatible | Tiene `/dev/gpiomem`, pero gpiozero/lgpio sigue siendo válido |
| Raspberry Pi Zero 2 W | ✅ Compatible | Mismo control GPIO; verifica alimentación del motor |
| Host Linux (sin GPIO) | ⚠️ Modo stub | `dispenser_mode: stub` o sin `/dev/gpiochip` → no toca pines, solo registra eventos |

> **Nota Pi 5:** el código **no** depende de `RPi.GPIO` (incompatible con Pi 5).
> El control se hace con `gpiozero` + backend `lgpio`. En entornos sin GPIO, el
> `Dispenser` degrada automáticamente a modo *stub* y la app sigue funcionando.
