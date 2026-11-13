# Datasheet — Vending Bebidas v2 (usuario/operador)

## Qué es

Máquina expendedora de **8 bebidas en lata** que acepta pagos con **USDC** sobre
Solana de forma instantánea (< 2 s). La v2 añade un **carrito de compra**: puedes
llevar hasta **1 unidad de cada bebida** (máximo 8 bebidas distintas) y pagar todo
junto con un único QR.

## Operación (cliente)

1. En el catálogo (columna izquierda) toca **«Añadir +»** en cada bebida que
   quieras. La tarjeta cambia a **«En carrito ✓»**.
2. En el carrito (columna derecha) revisa tu pedido: verás cada bebida, su
   subtotal y el **TOTAL**.
   - Para quitar una bebida usa el botón **−** (menos).
   - Para vaciar todo usa **«Vaciar carrito»**.
3. Toca **«Pagar»**. La máquina genera un QR y muestra:
   - El **monto total en grande** (con degradado),
   - el **resumen del pedido con la imagen de cada bebida** (nombre y precio),
   - y un **cronómetro circular** de cuenta regresiva junto al QR.
4. Escanea el QR con tu wallet (Phantom u otra compatible con Solana Pay). La
   wallet te pedirá el monto: **digita el total exacto** que muestra la pantalla
   (p. ej. `2.05` USDC).
5. Confirma el pago **antes de que el cronómetro llegue a cero** (60 s). La
   máquina detecta el pago **en menos de 2 segundos** y **dispensa cada bebida**
   secuencialmente (2.5 s por bebida).
6. La pantalla celebra el pago, el **carrito vuelve a cero** y en 15 s regresa al
   catálogo.

## Operación (operador)

### Salud de la máquina

```bash
curl http://<IP_DE_LA_PI>:8001/healthz
```

Compara que `status: ok`, `rpc_ok: true`, `gpio.mode: real` y `monitor: running`.

### Ver ventas / saldo de la máquina

```bash
cd ~/vending-solana-v2
.venv/bin/python scripts/sales.py          # resumen
.venv/bin/python scripts/sales.py --watch # en vivo cada 5 s
.venv/bin/python scripts/sales.py --last 10
```

### Devolver fondos (reembolso)

```bash
cd ~/vending-solana-v2
.venv/bin/python scripts/refund.py --to <WALLET_CLIENTE> --amount 2.05 --yes
```

### Cambiar precios

Edita `catalog.yaml` (precio en `price_usdc` y `price_base` en unidades base,
1 USDC = 1_000_000) y reinicia:

```bash
sudo systemctl restart vending-v2
```

### Probar el relé (dispensado manual)

```bash
cd ~/vending-solana-v2
.venv/bin/python scripts/dispense_test.py --pin 17 --seconds 2.5
```

## Solución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| La web no carga | Servicio caído | `sudo systemctl restart vending-v2`; `systemctl status vending-v2` |
| `/healthz` marca `rpc_ok: false` | Sin internet (módem LTE) | Revisa el módem Teltonika y `ping api.mainnet-beta.solana.com` |
| `gpio.mode: stub` (o `reason: GPIO busy`) | Otro proceso usa el GPIO (p. ej. la v1) | `sudo systemctl stop vending` (v1) y reinicia v2 |
| El cliente paga y no dispensa | Pago no detectado / monto fuera de tolerancia | Verifica el monto digitado (±0.01 USDC); revisa `journalctl -u vending-v2 -f` |
| Pago sin asociar | El cliente no escaneó el QR (pagó «enviando» manual sin reference) | Vé a la sección Pagos sin orden; devuelve manualmente con `refund.py` |

## Especificaciones

| Parámetro | Valor |
|---|---|
| Red | Solana **mainnet-beta** (USDC real) |
| Tokens aceptados | USDC (mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) |
| RPC | `https://api.mainnet-beta.solana.com` (con respaldo) |
| Detección de pago | < 2 s (reconciliación 2 s + WebSocket) |
| Timeout de orden | 60 s (cronómetro circular en pantalla) |
| Tolerancia de monto | ±0.01 USDC |
| Pulsos de dispensado | 2.5 s por bebida (GPIO 17), pausa 1 s entre bebidas |
| Catálogo | 8 bebidas, 0.80 – 1.50 USDC |
| Hardware | Raspberry Pi 5 (aarch64, Python 3.11), relé+LEDs, módem LTE Teltonika |

## Advertencias

- **Mainnet = dinero real**: cada venta mueve USDC reales del cliente a la ATA de
  la máquina.
- La clave privada (`wallet.json`) y el `.env` **nunca** deben subirse a GitHub ni
  compartirse.