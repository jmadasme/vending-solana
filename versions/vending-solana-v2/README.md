# 🥤 Vending Bebidas v2 — Máquina expendedora cripto con carrito

**Máquina expendedora de 8 bebidas en lata** que acepta **USDC (Solana) al
instante**. Versión 2 del proyecto [`vending-solana`](https://github.com/wanderc137/vending-solana):
grilla de productos, **carrito de compra**, pago con un solo QR por el **total
del pedido** y **dispensado secuencial**.

> ⚠️ **Dinero real**: este proyecto opera sobre **SOLANA MAINNET**. Los pagos son
> en USDC reales. No uses fondos que no quieras gastar.

---

## ✨ Novedades de la v2

- 🛒 **Carrito de compra** (columna derecha fija) + **grilla de 8 bebidas**
  (columna izquierda, distribución 4×2).
- 🥤 **8 bebidas**: Coca-Cola, Fanta, Sprite, Limón Soda, Agua sin gas, Agua con
  gas y Té helado — precios **semejantes pero distintos** (0.80 – 1.50 USDC).
- 🏪 **Resumen visual del pedido**: al pagar se muestra la **imagen de cada lata**
  seleccionada, su nombre y precio — el cliente ve exactamente qué está pagando.
- 🎯 **Máximo 1 unidad por producto** en el carrito.
- ⏳ **Cronómetro circular**: reloj de cuenta regresiva (anillo que se agota)
  sincronizado con el tiempo restante de la orden (60 s), junto al QR.
- 💰 **Monto protagonista**: el total a pagar se muestra en grande con degradado
  de marca, claro e inconfundible.
- 💳 **Un solo QR** por el total del pedido (sin `amount`: el cliente digita el
  total en su wallet).
- 🤖 **Dispensado secuencial**: un pulso de 2.5 s por cada bebida del carrito
  (GPIO 17).
- 🧹 Al confirmar el pago, el carrito **vuelve a cero**.

## 🔍 Principio de funcionamiento

1. El cliente **añade bebidas** a su carrito (1 por producto, sin límite de
   productos distintos).
2. Toca **«Pagar»**: el backend valida los skus, **recalcula el total** desde el
   catálogo y crea una orden con un `reference` único.
3. La pantalla muestra un **QR** `solana:<wallet>?spl-token=[REDACTED]
   (sin monto para evitar bugs de parseo de wallets) junto a un **cronómetro
   circular** con la cuenta regresiva (60 s), el **monto total en grande** y el
   **resumen del pedido con las imágenes** de cada bebida.
4. El cliente escanea el QR y **digita el total** en su wallet (p. ej. 3.60 USDC).
5. El monitor detecta el pago en **< 2 s** (reconciliación cada 2 s + WebSocket),
   valida el monto (±0.01 USDC) o el `reference`, y **dispensa secuencialmente**
   cada bebida comprada.
6. La orden queda `dispensada`, el carrito se limpia y la pantalla celebra.

## 🧾 Catálogo (8 bebidas)

| # | Producto | Precio USDC | Unidades base (6 dec.) |
|---|---|---|---|
| 1 | Coca-Cola (lata 330 ml) | 1.00 | 1_000_000 |
| 2 | Fanta (lata 330 ml) | 0.95 | 950_000 |
| 3 | Sprite (lata 330 ml) | 1.05 | 1_050_000 |
| 4 | Limón Soda (lata 330 ml) | 0.90 | 900_000 |
| 5 | Agua sin gas (lata 500 ml) | 0.80 | 800_000 |
| 6 | Agua con gas (lata 500 ml) | 0.85 | 850_000 |
| 7 | Bebida energética (lata 250 ml) | 1.50 | 1_500_000 |
| 8 | Té helado (lata 330 ml) | 1.20 | 1_200_000 |

*Los precios se definen en `catalog.yaml`.*

## 📌 Tabla de pines GPIO

| Pin BCM | Modo | Componente |
|--------:|------|------------|
| **17** | OUTPUT | **Relé / motor** — pulso 2.5 s por bebida dispensada |
| 27 | OUTPUT | LED «listo» |
| 22 | OUTPUT | LED «pagado» |
| 23 | OUTPUT | LED «error» |

```
        ┌─────────────────────────────┐
        │   Raspberry Pi 5            │
        │                             │
        │  GPIO 17 ──▶ Relé ──▶ Motor │  (2.5 s por bebida)
        │  GPIO 27 ──▶ LED  listo     │
        │  GPIO 22 ──▶ LED  pagado    │
        │  GPIO 23 ──▶ LED  error     │
        │                             │
        │  Módem LTE Teltonika (USB)  │ ◀── internet
        └─────────────────────────────┘
```

> Pi 5 usa **libgpiod** (`/dev/gpiochip4`), sin `/dev/gpiomem`. El backend GPIO es
> `gpiozero` + **lgpio**. En hosts sin GPIO el sistema cae a modo *stub*.

## 🚀 Despliegue en la Raspberry Pi

```bash
# 1. Copiar el proyecto (desde tu ordenador)
scp -r vending-solana-v2 jma@<IP_DE_LA_PI>:~/vending-solana-v2

# 2. Entorno Python (en la Pi)
cd ~/vending-solana-v2
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Wallet del comercio (NUNCA en git)
#    Copia tu keypair a ~/vending-solana-v2/wallet.json  (chmod 600)

# 4. Configuración: .env (plantilla en .env.example) — mainnet por defecto

# 5. Servicio systemd (puerto 8001 para no chocar con la v1)
sudo tee /etc/systemd/system/vending-v2.service > /dev/null <<'EOF'
[Unit]
Description=Vending Bebidas v2
After=network-online.target
[Service]
Type=simple
User=<USUARIO_PI>
WorkingDirectory=/home/<USUARIO_PI>/vending-solana-v2
EnvironmentFile=/home/<USUARIO_PI>/vending-solana-v2/.env
ExecStart=/home/<USUARIO_PI>/vending-solana-v2/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8001
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now vending-v2
```

La web queda en `http://<IP_DE_LA_PI>:8001`.

> ⚠️ Solo **un proceso** puede controlar el GPIO. Si conviven v1 y v2, detén uno
> (`sudo systemctl stop vending`) para que el otro tenga acceso real al relé.

## 🧪 Pruebas

```bash
# Crear una orden con 2 bebidas (total 2.05 USDC)
curl -s -X POST http://localhost:8001/order \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"sku":"coca-cola"},{"sku":"sprite"}]}'

# Estado de la orden
curl -s http://localhost:8001/order/<ORDER_ID>

# Salud
curl -s http://localhost:8001/healthz
```

## 🛡️ Seguridad

- La clave privada del comercio **nunca** se sube a git (`wallet.json`, `.env`,
  `*.key` en `.gitignore`).
- El backend **recalcula el total** desde el catálogo (no confía en el cliente).
- Validación de pago: monto exacto (±0.01 USDC) o `reference` único (anti-replay).
- Headers de seguridad (CSP, X-Frame-Options…) y rate-limit en `POST /order`.
- Emparentamiento por monto: si wallet no envía `reference`, se casa con la orden
  pendiente más antigua cuyo total coincida dentro de tolerancia.

## 🧰 Scripts

| Script | Uso |
|---|---|
| `scripts/sales.py` | Visor de ventas (solo lectura): saldo de la ATA y transacciones |
| `scripts/refund.py` | Devolver fondos: `refund.py --to <wallet> --amount <USDC> --yes` |
| `scripts/dispense_test.py` | Probar el relé: `dispense_test.py --pin 17 --seconds 2.5` |
| `scripts/devnet_e2e_test.py` | Test E2E en devnet |

## 📁 Estructura

```
vending-solana-v2/
├── app.py              # FastAPI: grilla, carrito (POST /order multi-item), QR, monitor
├── payments.py         # Solana Pay: Transfer Request sin amount, validación, failover
├── dispenser.py        # GPIO: relé + LEDs (gpiozero/lgpio) con stub de desarrollo
├── wallet.py           # Keypair + ATA USDC
├── config.py           # Configuración (env > .env > config.yaml)
├── catalog.yaml        # 8 bebidas y precios
├── config.yaml         # mainnet, RPC, timeout 60 s, pines
├── templates/          # index (grilla+carrito), payment (QR+cronómetro), confirmed, cancelled, error
├── static/             # style.css + 8 imágenes de latas
└── scripts/            # sales.py, refund.py, dispense_test.py, devnet_e2e_test.py
```

## ⚠️ Advertencias

- **Mainnet = dinero real**: prueba primero en devnet si no tienes experiencia.
- La máquina física v2 usa **un solo motor** (GPIO 17): las 8 bebidas se despachan
  por el mismo compartimiento de forma **secuencial**.
- El QR **no incluye el monto** a propósito (algunas wallets como Phantom mal
  parsean `amount`). El cliente digita el total manualmente.
- El **cronómetro de la pantalla de pago** está sincronizado con el tiempo real
  de la orden (60 s por defecto, `order_timeout_seconds` en `config.yaml`);
  cuando llega a cero la página vuelve sola al catálogo.