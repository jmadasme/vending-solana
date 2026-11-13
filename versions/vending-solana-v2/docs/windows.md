# Windows 10/11 — Despliegue del vending

Port del proyecto (originalmente Raspberry Pi 5) a un PC con Windows 10/11.
La web, el carrito, el QR Solana Pay, el monitor de pagos y el dispensado
secuencial funcionan igual. Cambian solo las capas específicas del hardware.

## Diferencias con la Raspberry Pi

| Función | Raspberry Pi | Windows |
|---|---|---|
| GPIO (relé/LEDs) | `gpiozero` + `lgpio` | **Stub**: no hay GPIO; el pulso se registra en el log |
| Servicio | systemd | Scripts de PowerShell (o Tarea programada) |
| Kiosko | Chromium `--kiosk` | Microsoft Edge `--kiosk` |
| Wallet | keypair completo | **Watch-only**: solo dirección pública (cobrar sí, firmar no) |
| Módem LTE | wvdial/ppp | No aplica (usar la red del PC) |

## Requisitos

- Windows 10/11 x64, Python 3.12, Microsoft Edge.
- Conexión a internet (RPC de Solana mainnet).
- Opcional: pantalla para el kiosko.

## 1. Instalar Python 3.12 (una vez)

```powershell
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
```

Cerrar y reabrir la terminal para que `python` esté en el PATH.

## 2. Preparar el entorno

Copiar el proyecto a `C:\vending-solana-v2` y ejecutar:

```powershell
cd C:\vending-solana-v2
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

Crea `.venv`, instala `requirements-windows.txt` (sin `gpiozero`/`lgpio`) y
genera `.env` si no existe.

## 3. Wallet del comercio (watch-only)

`wallet.json` acepta tres formatos:

```json
{"pubkey": "DIRECCION_PUBLICA_DEL_COMERCIO"}
```

También una dirección base58 suelta o la lista de 64 bytes de un keypair.
Con `{"pubkey": ...}` la app **no puede firmar** (no crea la ATA ni hace
refunds); solo cobra en la ATA USDC existente. Para devolver fondos hay que
usar la wallet propietaria (Phantom) que tenga la clave.

La ATA de USDC debe existir antes de cobrar. Se puede verificar en un explorer
con la dirección del comercio.

`.env` recomendado en Windows:

```dotenv
WALLET_PATH=wallet.json
DISPENSER_MODE=stub
TEST_TOKEN=[REDACTED]
```

`config.yaml` ya apunta a mainnet y al mint real de USDC.

## 4. Ejecutar

```powershell
cd C:\vending-solana-v2
powershell -ExecutionPolicy Bypass -File .\run_windows.ps1
```

Web: `http://127.0.0.1:8000` (y `http://<IP-del-PC>:8000` en la LAN).
El kiosko se abre con:

```powershell
powershell -ExecutionPolicy Bypass -File .\kiosk_windows.ps1
```

Salir del kiosko: `Alt+F4` o `Ctrl+Alt+Del`.

## 5. Pruebas

```powershell
# Salud (RPC, wallet, modo del dispensador)
curl.exe -s http://127.0.0.1:8000/healthz

# Crear orden de 2 bebidas (2.05 USDC)
curl.exe -s -X POST http://127.0.0.1:8000/order -H "Content-Type: application/json" -d '{\"items\":[{\"sku\":\"coca-cola\"},{\"sku\":\"sprite\"}]}'

# Pulso de prueba del dispensador (stub) — requiere TEST_TOKEN
curl.exe -s -X POST http://127.0.0.1:8000/api/test/dispense -H "X-Test-Token: <token>" -H "Content-Type: application/json" -d '{\"pin\":17,\"seconds\":1}'
```

En `healthz`, `gpio.mode` debe ser `stub` y `signing` debe ser `false`.

## 6. Arranque automático (opcional)

Crear una tarea al iniciar sesión (sin privilegios de administrador):

```powershell
schtasks /Create /TN "VendingSolana" /SC ONLOGON /TR "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\vending-solana-v2\run_windows.ps1" /F
```

Para levantar también el kiosko, apuntar la tarea a un script que ejecute
`run_windows.ps1` en segundo plano y luego `kiosk_windows.ps1`.

## Limitaciones

- **Sin dispensado físico**: `dispenser_mode=stub` registra el pulso en el log.
  Para hardware real hace falta un relé USB-serial o un puente ESP8266/Arduino
  y un adaptador en `dispenser.py`.
- **Sin firma**: la wallet watch-only no crea ATAs ni reembolsa. Refunds con
  `scripts/refund.py` requieren un `wallet.json` con clave privada.
- El estado de las órdenes vive en memoria: reiniciar el servicio borra las
  órdenes pendientes (igual que en la Pi).
