# config.py — Carga de configuración (config.yaml + .env + env vars).
# Preferencia: variables de entorno > .env > config.yaml.
"""Carga la configuración de la máquina expendedora.

Fuentes (de mayor a menor prioridad):
1. Variables de entorno del proceso (p.ej. RPC_HTTP=...)
2. Archivo .env (cargado con python-dotenv)
3. config.yaml
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    network: str = "devnet"
    rpc_http: str = "https://api.devnet.solana.com"
    rpc_ws: str = "wss://api.devnet.solana.com"
    rpc_http_backup: str = "https://api.devnet.solana.com"
    usdc_mint: str = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    usdc_decimals: int = 6
    wallet_path: str = "wallet.json"
    order_timeout_seconds: int = 300
    dispense_pulse_seconds: float = 2.5
    poll_interval_ms: int = 1500
    dispense_pin: int = 17
    gpio_chip: int | None = None
    led_ready_pin: int = 27
    led_paid_pin: int = 22
    led_error_pin: int = 23
    dispenser_mode: str = "auto"
    test_token: str = ""
    auto_refund_enabled: bool = False
    refund_delay_seconds: int = 30
    refund_to: str = ""
    base_dir: Path = field(default=BASE_DIR)

    @property
    def wallet_abs_path(self) -> Path:
        p = Path(self.wallet_path)
        return p if p.is_absolute() else self.base_dir / p


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")

    cfg = Config()

    yaml_path = BASE_DIR / "config.yaml"
    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text()) or {}
        for key, value in data.items():
            if hasattr(cfg, key) and key != "base_dir":
                setattr(cfg, key, value)

    # Sobrescribir con variables de entorno / .env
    env_map = {
        "RPC_HTTP": "rpc_http",
        "RPC_WS": "rpc_ws",
        "RPC_HTTP_BACKUP": "rpc_http_backup",
        "WALLET_PATH": "wallet_path",
        "USDC_MINT": "usdc_mint",
        "USDC_DECIMALS": "usdc_decimals",
        "ORDER_TIMEOUT_SECONDS": "order_timeout_seconds",
        "DISPENSE_PULSE_SECONDS": "dispense_pulse_seconds",
        "DISPENSE_PIN": "dispense_pin",
        "GPIO_CHIP": "gpio_chip",
        "TEST_TOKEN": "test_token",
        "DISPENSER_MODE": "dispenser_mode",
        "AUTO_REFUND_ENABLED": "auto_refund_enabled",
        "REFUND_DELAY_SECONDS": "refund_delay_seconds",
        "REFUND_TO": "refund_to",
    }
    for env_name, attr in env_map.items():
        value = os.getenv(env_name)
        if value is None or value == "":
            continue
        if attr == "usdc_decimals":
            setattr(cfg, attr, int(value))
        elif attr in ("order_timeout_seconds", "refund_delay_seconds"):
            setattr(cfg, attr, int(value))
        elif attr == "auto_refund_enabled":
            setattr(cfg, attr, value.strip().lower() in ("1", "true", "yes", "si", "sí", "on"))
        elif attr in ("dispense_pulse_seconds",):
            setattr(cfg, attr, float(value))
        elif attr in ("dispense_pin", "gpio_chip"):
            setattr(cfg, attr, int(value))
        else:
            setattr(cfg, attr, value)

    cfg.base_dir = BASE_DIR
    return cfg