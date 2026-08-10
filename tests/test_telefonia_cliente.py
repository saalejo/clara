"""El cliente del agente contra el puente, sin puente de verdad.

Dos cosas que se fijan aquí y que costaron una tarde cada una:

1. **El canal de eventos no puede tener tope de lectura.** Es un flujo que dura
   lo que dure el agente.
2. **Ningún fallo puede escapar como excepción cruda** hacia una herramienta.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from voice_agent.telefonia import ClienteTelefonia, ErrorTelefonia


@pytest.fixture
def cliente(tmp_path: Path) -> ClienteTelefonia:
    return ClienteTelefonia(tmp_path / "telefonia.sock", timeout_s=4.0)


class TestTiemposDeEspera:
    def test_las_peticiones_normales_llevan_tope(self, cliente: ClienteTelefonia) -> None:
        """Mientras una herramienta corre, el agente calla: no puede esperar
        indefinidamente."""
        c = cliente._cliente(cliente.timeout_s)
        assert c.timeout.read == 4.0
        assert c.timeout.connect == 4.0

    def test_el_canal_de_eventos_no_lleva_tope_de_lectura(self, cliente: ClienteTelefonia) -> None:
        """EL test de este fichero.

        Con un tope de lectura, el SSE se caía cada cuatro segundos y el agente
        se pasaba la vida reconectando, registrando `canal de eventos caído ()`
        —sin mensaje, porque el `str()` de un `httpx.ReadTimeout` está vacío—.
        El de conexión sí se conserva: un puente que no está debe detectarse ya.
        """
        espera = httpx.Timeout(cliente.timeout_s, read=None)
        c = cliente._cliente(espera)
        assert c.timeout.read is None
        assert c.timeout.connect == 4.0


class TestLosFallosSeTraducen:
    async def test_sin_puente_da_error_con_sugerencia(self, cliente: ClienteTelefonia) -> None:
        with pytest.raises(ErrorTelefonia) as e:
            await cliente.estado()
        assert e.value.sugerencia

    async def test_disponible_nunca_lanza(self, cliente: ClienteTelefonia) -> None:
        """Es lo que sondea `bot.py` al arrancar: si lanzara, el agente entero
        no arrancaría cuando el puente no está."""
        assert await cliente.disponible() is False
