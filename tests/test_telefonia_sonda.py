"""La lectura de `+CLCC`, que es lo que la sonda existe para averiguar.

`+CLCC` es la única fuente que dice **la dirección** de una llamada sin
adivinarla: `dir` 1 es entrante y `stat` 4 es `incoming`. Si con una llamada de
WhatsApp sale `1,4` mientras oFono la traduce como `alerting`, el móvil la está
presentando bien y el problema es de oFono — que es justo la hipótesis abierta.

Por eso se prueba el traductor y no el socket: equivocarse aquí es sacar la
conclusión contraria de una medida buena.
"""

from __future__ import annotations

import pytest

from voice_agent_telefonia.sonda import SALUDO, explicar_clcc


class TestExplicarCLCC:
    def test_una_entrante_de_verdad_se_reconoce(self) -> None:
        """`dir=1, stat=4`: el caso que resolvería el problema."""
        lineas = explicar_clcc('+CLCC: 1,1,4,0,0,"10000000",129')

        assert "ENTRANTE" in lineas[0]
        assert "incoming" in lineas[0]

    def test_una_entrante_de_verdad_lo_dice_bien_alto(self) -> None:
        """El hallazgo no puede pasar desapercibido en medio de la traza."""
        lineas = explicar_clcc("+CLCC: 1,1,4,0,0")

        assert len(lineas) == 2
        assert "ATA" in lineas[1]

    def test_una_saliente_no_lleva_el_aviso(self) -> None:
        lineas = explicar_clcc('+CLCC: 1,0,2,0,0,"+573001234567",129')

        assert "SALIENTE" in lineas[0]
        assert "marcando" in lineas[0]
        assert len(lineas) == 1

    @pytest.mark.parametrize(
        ("stat", "esperado"),
        [("0", "activa"), ("1", "retenida"), ("2", "marcando"), ("3", "sonando")],
    )
    def test_los_demas_estados_se_traducen(self, stat: str, esperado: str) -> None:
        """`alerting`/`dialing` son los que hemos visto: tienen que salir claros."""
        assert esperado in explicar_clcc(f"+CLCC: 1,1,{stat},0,0")[0]

    def test_un_estado_desconocido_no_se_inventa(self) -> None:
        """Mejor un `?9` visible que una traducción falsa en una medida."""
        assert "?9" in explicar_clcc("+CLCC: 1,1,9,0,0")[0]

    def test_las_lineas_que_no_son_clcc_se_ignoran(self) -> None:
        assert explicar_clcc("RING") == []
        assert explicar_clcc("+CIEV: 2,1") == []
        assert explicar_clcc("OK") == []

    def test_un_clcc_truncado_no_revienta(self) -> None:
        """Llega por radio y se parte por trozos: la sonda no puede caerse."""
        assert explicar_clcc("+CLCC: 1") == []
        assert explicar_clcc("+CLCC:") == []


class TestSaludo:
    def test_cmer_va_despues_de_cind(self) -> None:
        """Sin `AT+CIND=?` antes, los índices de los `+CIEV` no significan nada."""
        assert SALUDO.index("AT+CIND=?") < SALUDO.index("AT+CMER=3,0,0,1")

    def test_se_pide_el_identificador_de_llamada(self) -> None:
        """Sin `AT+CLIP=1` no llega el número, y es lo que distingue una app."""
        assert "AT+CLIP=1" in SALUDO
