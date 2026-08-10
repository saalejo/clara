"""El retén de llamadas de la compuerta de micrófono.

Mientras hay una llamada en curso, la persona habla por teléfono y no con el
agente: lo que capte el micrófono de la sala es media conversación ajena que
el VAD tomaría por preguntas. El retén cierra la compuerta al margen del ciclo
habla/calla del bot, y lo que se fija aquí es justo esa independencia: ninguno
de los dos cierres puede pisar al otro.
"""

from __future__ import annotations

from voice_agent.audio_gate import MicrophoneGate


class TestRetenDeLlamada:
    """El retén de llamadas: independiente del ciclo habla/calla del bot."""

    def test_retener_cierra_y_soltar_abre(self) -> None:
        gate = MicrophoneGate(hangover_secs=0)
        assert not gate.cerrada
        gate.retener()
        assert gate.cerrada
        gate.soltar()
        assert not gate.cerrada

    def test_el_reten_sobrevive_al_ciclo_del_bot(self) -> None:
        """Que el bot hable y calle durante la llamada no suelta el retén."""
        gate = MicrophoneGate(hangover_secs=0)
        gate.retener()
        gate.cerrar()
        gate.abrir_tras_cola()
        assert gate.cerrada

    def test_soltar_no_interrumpe_el_cierre_por_habla(self) -> None:
        """Soltar el retén con el bot hablando deja la compuerta cerrada."""
        gate = MicrophoneGate(hangover_secs=0)
        gate.cerrar()
        gate.retener()
        gate.soltar()
        assert gate.cerrada

    async def test_retenida_devuelve_silencio(self) -> None:
        gate = MicrophoneGate(hangover_secs=0)
        gate.retener()
        assert await gate.filter(b"\x01\x02\x03\x04") == b"\x00\x00\x00\x00"
