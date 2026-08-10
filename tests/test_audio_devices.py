"""La tolerancia a que la tarjeta de sonido no esté, o vaya y venga.

Lo que se fija aquí es el contrato del modo enchufar-y-listo: sin tarjeta el
agente no muere —espera—, con tarjeta arranca a la primera, y si el juego de
tarjetas cambia con el pipeline en marcha, el vigilante lo para de forma
ordenada. La lectura de `/proc/asound/cards` se prueba con el formato real del
kernel, incluida la línea `--- no soundcards ---` que escribe cuando no hay
ninguna.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pipecat.pipeline.worker import PipelineWorker

import voice_agent.audio_devices as audio_devices
import voice_agent.bot as bot
from voice_agent.audio_devices import (
    AudioDeviceError,
    esperar_dispositivos,
    extraer_tarjetas,
    tarjetas_alsa,
)
from voice_agent_core.config import Settings


def settings_prueba() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestExtraerTarjetas:
    def test_una_tarjeta_con_formato_real(self) -> None:
        contenido = (
            " 0 [Device         ]: USB-Audio - USB PnP Sound Device\n"
            "                      C-Media Electronics Inc. USB PnP Sound Device "
            "at usb-fe3a0000.usb-1, full speed\n"
        )
        assert extraer_tarjetas(contenido) == frozenset({"Device"})

    def test_varias_tarjetas(self) -> None:
        contenido = (
            " 0 [Device         ]: USB-Audio - USB PnP Sound Device\n"
            "                      texto de detalle\n"
            " 1 [Dummy          ]: Dummy - Dummy\n"
            "                      Dummy 1\n"
        )
        assert extraer_tarjetas(contenido) == frozenset({"Device", "Dummy"})

    def test_sin_tarjetas(self) -> None:
        # Es literalmente lo que escribe el kernel cuando no hay ninguna.
        assert extraer_tarjetas("--- no soundcards ---\n") == frozenset()

    def test_vacio(self) -> None:
        assert extraer_tarjetas("") == frozenset()

    def test_sin_proc_asound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un sistema sin driver de sonido cuenta como cero tarjetas, no como error."""
        monkeypatch.setattr(
            audio_devices, "RUTA_TARJETAS_ALSA", audio_devices.RUTA_TARJETAS_ALSA / "no-existe"
        )
        assert tarjetas_alsa() == frozenset()


class TestEsperarDispositivos:
    async def test_con_tarjeta_resuelve_a_la_primera(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(audio_devices, "tarjetas_alsa", lambda: frozenset({"Device"}))
        monkeypatch.setattr(audio_devices, "resolve_device_indices", lambda _s: (0, 0))

        assert await esperar_dispositivos(settings_prueba(), intervalo_secs=0) == frozenset(
            {"Device"}
        )

    async def test_espera_hasta_que_aparece(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin tarjeta no revienta: sondea hasta que la enchufan."""
        sondeos: list[frozenset[str]] = [frozenset(), frozenset(), frozenset({"Device"})]
        monkeypatch.setattr(audio_devices, "tarjetas_alsa", lambda: sondeos.pop(0))
        monkeypatch.setattr(audio_devices, "resolve_device_indices", lambda _s: (0, 0))

        assert await esperar_dispositivos(settings_prueba(), intervalo_secs=0) == frozenset(
            {"Device"}
        )
        assert not sondeos

    async def test_tarjeta_equivocada_sigue_esperando(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hay tarjeta pero los dispositivos no resuelven: se espera a la buena."""
        sondeos: list[frozenset[str]] = [frozenset({"Otra"}), frozenset({"Otra", "Device"})]
        monkeypatch.setattr(audio_devices, "tarjetas_alsa", lambda: sondeos.pop(0))

        intentos: list[frozenset[str]] = []

        def resolver(_settings: Settings) -> tuple[int, int]:
            intentos.append(frozenset())
            if len(intentos) == 1:
                raise AudioDeviceError("no está la tarjeta configurada")
            return (0, 0)

        monkeypatch.setattr(audio_devices, "resolve_device_indices", resolver)

        resultado = await esperar_dispositivos(settings_prueba(), intervalo_secs=0)
        assert resultado == frozenset({"Otra", "Device"})
        assert len(intentos) == 2


class WorkerFalso:
    """Solo apunta si lo cancelaron y con qué motivo."""

    def __init__(self) -> None:
        self.cancelado_con: str | None = None

    async def cancel(self, *, reason: str | None = None) -> None:
        self.cancelado_con = reason


class TestVigilarTarjetas:
    async def test_cancela_cuando_cambia_el_juego(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Quitar la tarjeta —o cambiarla por otra— para el pipeline con motivo."""
        sondeos: list[frozenset[str]] = [frozenset({"Device"}), frozenset()]
        monkeypatch.setattr(bot, "tarjetas_alsa", lambda: sondeos.pop(0))

        worker = WorkerFalso()
        await bot._vigilar_tarjetas(
            cast(PipelineWorker, cast(Any, worker)), frozenset({"Device"}), intervalo_secs=0
        )
        assert worker.cancelado_con == "tarjeta de sonido desconectada"
