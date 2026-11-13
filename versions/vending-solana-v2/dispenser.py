# dispenser.py — Control GPIO del relé/motor y LEDs de estado.
"""Dispensador con gpiozero (backend lgpio, compatible Raspberry Pi 5).

La Pi 5 NO tiene /dev/gpiomem: usa libgpiod (/dev/gpiochip4). gpiozero con el
backend lgpio detecta automáticamente el chip. En un host de desarrollo sin
GPIO el controlador entra en modo *stub* y registra los eventos en el log en
lugar de tocar pines, de modo que la aplicación nunca falla por falta de GPIO.

Pines (BCM):
- dispense_pin (17): relé + motor, activo-alto, pulso de dispense_pulse_seconds (2.5 s).
- led_ready_pin (27): LED "listo".
- led_paid_pin  (22): LED "pagado".
- led_error_pin (23): LED "error".
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

try:
    from gpiozero import OutputDevice  # type: ignore

    _GPIOZERO_OK = True
except Exception:  # pragma: no cover
    _GPIOZERO_OK = False


class DispenserError(RuntimeError):
    pass


class Dispenser:
    """Controlador del dispensador. `stub=True` no toca GPIO (host de dev)."""

    def __init__(self, config: Config, force_stub: bool = False):
        self.config = config
        self.available = False
        self.stub = force_stub or config.dispenser_mode == "stub"
        self.stub_reason: str = ""
        self._relay: Optional[OutputDevice] = None
        self._leds: dict[str, OutputDevice] = {}
        self._lock = threading.Lock()
        self._dispensing = False
        self._init_gpio()

    # ------------------------------------------------------------------
    def _init_gpio(self) -> None:
        if self.stub:
            self.stub_reason = "modo stub forzado por config (dispenser_mode=stub)"
            logger.info("Dispenser en modo STUB (sin GPIO).")
            return
        if not _GPIOZERO_OK:
            self.stub_reason = "gpiozero no instalado"
            logger.warning("gpiozero no disponible -> modo STUB.")
            self.stub = True
            return
        try:
            # I-010: si lgpio no está instalado, la Pi 5 no puede tocar GPIO
            # (no tiene /dev/gpiomem; usa libgpiod). Se reporta el motivo.
            try:
                from gpiozero.pins.lgpio import LGPIOFactory  # type: ignore
            except ImportError as exc:  # pragma: no cover
                self.stub_reason = (
                    "lgpio no instalado (requerido en Pi 5: pip install lgpio / "
                    "sudo apt-get install python3-lgpio)"
                )
                raise exc

            # En la Pi 5 con RP1 el header de 40 pines es /dev/gpiochip15.
            # Sin `chip`, lgpio busca chips que no corresponden al header.
            if self.config.gpio_chip is None:
                factory = LGPIOFactory()  # auto-detección (otras Pi)
            else:
                factory = LGPIOFactory(chip=self.config.gpio_chip)
            self._relay = OutputDevice(self.config.dispense_pin, active_high=True, pin_factory=factory)
            self._leds = {
                "ready": OutputDevice(self.config.led_ready_pin, active_high=True, pin_factory=factory),
                "paid": OutputDevice(self.config.led_paid_pin, active_high=True, pin_factory=factory),
                "error": OutputDevice(self.config.led_error_pin, active_high=True, pin_factory=factory),
            }
            self.available = True
            self.stub_reason = ""
            self._set_led_sync("ready", True)
            logger.info(
                "GPIO disponible (chip=%s). Relé en pin %d, LEDs en %d/%d/%d",
                factory.chip if hasattr(factory, "chip") else "?",
                self.config.dispense_pin,
                self.config.led_ready_pin,
                self.config.led_paid_pin,
                self.config.led_error_pin,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GPIO no disponible (%s) -> modo STUB.", exc)
            self._relay = None
            self._leds = {}
            self.available = False
            self.stub = True
            if not self.stub_reason:
                self.stub_reason = str(exc)

    # ------------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return self.available

    def _set_led_sync(self, name: str, on: bool) -> None:
        if self.stub:
            logger.info("[STUB] LED %s -> %s", name, "ON" if on else "OFF")
            return
        led = self._leds.get(name)
        if led is not None:
            if on:
                led.on()
            else:
                led.off()

    def set_led(self, name: str, on: bool) -> None:
        """Enciende/apaga un LED ('ready'|'paid'|'error') de forma síncrona."""
        self._set_led_sync(name, on)

    async def set_led_async(self, name: str, on: bool) -> None:
        await asyncio.to_thread(self._set_led_sync, name, on)

    # ------------------------------------------------------------------
    def _dispense_blocking(self, pin: int, seconds: float) -> None:
        """Pulso de `seconds` en el relé (activo-alto). Con bloqueo anti-doble."""
        with self._lock:
            if self._dispensing:
                logger.warning("Dispensado ya en curso; ignorando disparo duplicado.")
                return
            self._dispensing = True
        try:
            if self.stub:
                logger.info("[STUB] Dispensado: relé pin %d ON %.1fs", pin, seconds)
                time.sleep(min(seconds, 0.5))  # no bloquear mucho en pruebas
                return
            if self._relay is None:
                raise DispenserError("Dispensador no inicializado (sin GPIO)")
            self._set_led_sync("paid", True)
            self._relay.on()
            time.sleep(seconds)
            self._relay.off()
            self._set_led_sync("paid", False)
            logger.info("Dispensado completado (pin %d, %.1fs)", pin, seconds)
        finally:
            with self._lock:
                self._dispensing = False

    async def dispense(self, pin: Optional[int] = None, seconds: Optional[float] = None) -> None:
        """Dispara el dispensado. Devuelve al completar el pulso."""
        pin = pin if pin is not None else self.config.dispense_pin
        seconds = seconds if seconds is not None else self.config.dispense_pulse_seconds
        await asyncio.to_thread(self._dispense_blocking, pin, seconds)

    def dispense_sync(self, pin: Optional[int] = None, seconds: Optional[float] = None) -> None:
        pin = pin if pin is not None else self.config.dispense_pin
        seconds = seconds if seconds is not None else self.config.dispense_pulse_seconds
        self._dispense_blocking(pin, seconds)

    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        if self._relay is not None:
            try:
                self._relay.off()
            except Exception:  # noqa: BLE001
                pass
        for led in self._leds.values():
            try:
                led.off()
            except Exception:  # noqa: BLE001
                pass
        self._relay = None
        self._leds = {}


# Acceso "de un solo uso" para pruebas CLI rápidas
def dispense(pin: int, seconds: float) -> None:
    """Función CLI: pulso de prueba. Ej: python -c "from dispenser import dispense; dispense(17, 2.5)" """
    from config import load_config

    d = Dispenser(load_config())
    try:
        d.dispense_sync(pin, seconds)
    finally:
        d.cleanup()