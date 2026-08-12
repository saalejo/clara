"""Las herramientas de agenda: lo que el modelo ve y lo que puede hacer con ellas.

Dos contratos. El primero, que el catálogo sigue siendo **aditivo**: sin la
bandera de agenda el modelo ve exactamente las mismas herramientas que antes de
que esto existiera, porque anunciar una que no puede funcionar es lo que hace
que el modelo diga que la ha usado.

El segundo, las reglas de la propia herramienta: que no inventa un número, que
no agenda en el pasado, y que ningún fallo se escapa como excepción — un error
es un dato que el modelo tiene que poder contarle a la persona, no algo que
rompa el turno.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper

from voice_agent.misiones_agente import AlmacenMisiones
from voice_agent.resources import AppResources
from voice_agent.tools import HERRAMIENTAS, HERRAMIENTAS_AGENDA, herramientas_activas, nombre_de
from voice_agent.tools.misiones import (
    cancelar_llamada_programada,
    editar_llamada_programada,
    llamadas_programadas,
    programar_llamada,
)
from voice_agent_core.config import Settings
from voice_agent_core.misiones import cargar_misiones


@dataclass
class ParamsFalsos:
    """Sustituto de `FunctionCallParams` con lo único que estas usan."""

    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def recursos_con(
    tmp_path: Path | None = None, *, numero_llamada: str = ""
) -> tuple[ParamsFalsos, AlmacenMisiones | None]:
    settings = Settings(_env_file=None, data_dir=tmp_path or Path("/no/existe"))  # type: ignore[call-arg]
    almacen = AlmacenMisiones(settings) if tmp_path is not None else None
    recursos = AppResources(
        settings=settings,
        retriever=cast(Any, object()),
        almacen_misiones=almacen,
        numero_llamada=numero_llamada,
    )
    return ParamsFalsos(app_resources=recursos), almacen


def dentro_de(horas: int = 3) -> str:
    return f"{datetime.now() + timedelta(hours=horas):%Y-%m-%d %H:%M}"


class TestElCatalogo:
    def test_los_nombres_expuestos_al_modelo_son_los_esperados(self) -> None:
        nombres = {
            DirectFunctionWrapper(f).to_function_schema().to_default_dict()["name"]
            for f in cast(list[Any], HERRAMIENTAS_AGENDA)
        }
        assert nombres == {
            "programar_llamada",
            "editar_llamada_programada",
            "cancelar_llamada_programada",
            "llamadas_programadas",
        }

    def test_todas_tienen_descripcion(self) -> None:
        # El docstring de una herramienta es código: de él sale lo que el
        # modelo lee para decidir si la usa.
        for f in cast(list[Any], HERRAMIENTAS_AGENDA):
            esquema = DirectFunctionWrapper(f).to_function_schema().to_default_dict()
            assert esquema["description"].strip()

    def test_no_estan_en_el_catalogo_de_siempre(self) -> None:
        # Si esto se rompe, el agente sin agenda anunciaría herramientas que no
        # puede usar, y además caerían todos los tests de catálogo del resto.
        assert not set(HERRAMIENTAS) & set(HERRAMIENTAS_AGENDA)

    def test_sin_la_bandera_el_catalogo_es_el_de_siempre(self) -> None:
        assert herramientas_activas(frozenset()) == HERRAMIENTAS

    def test_con_la_bandera_se_suman_las_cuatro(self) -> None:
        activas = herramientas_activas(frozenset(), incluir_agenda=True)
        assert len(activas) == len(HERRAMIENTAS) + 4

    def test_se_pueden_apagar_desde_el_panel_como_cualquier_otra(self) -> None:
        activas = herramientas_activas({"programar_llamada"}, incluir_agenda=True)
        assert "programar_llamada" not in {nombre_de(h) for h in activas}
        assert "llamadas_programadas" in {nombre_de(h) for h in activas}


class TestProgramar:
    async def test_agenda_y_lo_deja_escrito(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar el control del día cinco.")
        assert params.resultado["programada"] is True
        # El lector real del panel: el round-trip es el contrato.
        assert len(cargar_misiones(tmp_path).pendientes) == 1

    async def test_usa_el_numero_de_la_llamada_en_curso(self, tmp_path: Path) -> None:
        # El caso "llámame a mí", que es el normal: el modelo no tiene que
        # saber ningún número.
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar el control.")
        assert cargar_misiones(tmp_path).pendientes[0].contacto_numero == "3046411802"

    async def test_el_numero_explicito_manda(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar.", "Nora", "3001112233")
        assert cargar_misiones(tmp_path).pendientes[0].contacto_numero == "3001112233"

    async def test_sin_numero_ni_llamada_no_inventa_nada(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path)
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar.")
        assert "error" in params.resultado
        assert cargar_misiones(tmp_path).pendientes == []

    async def test_sin_encargo_no_agenda(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "   ")
        assert "error" in params.resultado

    async def test_devuelve_la_fecha_redactada_en_espanol(self, tmp_path: Path) -> None:
        # Es lo que el agente dice en voz alta para que le puedan corregir.
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), "2026-12-25 17:30", "Retomar.")
        assert "de diciembre" in params.resultado["cuando"]
        assert "17:30" in params.resultado["cuando"]

    async def test_avisa_de_no_leer_el_identificador(self, tmp_path: Path) -> None:
        # `agenda-20261225-1730-a3f9` dicho por Piper es horrible.
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar.")
        assert "identificador" in params.resultado["aviso"]


class TestLosRechazos:
    @pytest.mark.parametrize("texto", ["mañana a las cinco", "en dos horas", ""])
    async def test_una_fecha_ininteligible_no_rompe_el_turno(
        self, tmp_path: Path, texto: str
    ) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), texto, "Retomar.")
        assert "error" in params.resultado
        assert params.resultado["sugerencia"]

    async def test_no_se_puede_programar_en_el_pasado(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(-3), "Retomar.")
        assert "error" in params.resultado
        assert cargar_misiones(tmp_path).pendientes == []

    async def test_no_se_puede_programar_para_dentro_de_un_instante(self, tmp_path: Path) -> None:
        # Nada puede nacer ya vencido: el planificador mira el reloj cada
        # treinta segundos, y durante una llamada no mira nada.
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        cuando = f"{datetime.now() + timedelta(seconds=30):%Y-%m-%d %H:%M}"
        await programar_llamada(cast(Any, params), cuando, "Retomar.")
        assert "error" in params.resultado

    async def test_no_se_puede_programar_dentro_de_diez_anos(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), "2036-08-13 17:00", "Retomar.")
        assert "error" in params.resultado

    async def test_sin_almacen_lo_dice_en_vez_de_reventar(self) -> None:
        params, _ = recursos_con(None, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar.")
        assert "error" in params.resultado


class TestEditarYCancelar:
    async def test_editar_mueve_la_hora(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar.")
        id_mision = params.resultado["id"]

        await editar_llamada_programada(cast(Any, params), id_mision, dentro_de(30))
        assert params.resultado["cambiada"] is True

    async def test_editar_sin_decir_que_cambiar_no_hace_nada(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await editar_llamada_programada(cast(Any, params), "agenda-lo-que-sea")
        assert "error" in params.resultado

    async def test_editar_un_id_desconocido_lo_dice(self, tmp_path: Path) -> None:
        # El modelo se inventa ids, y las tareas del panel no son suyas.
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await editar_llamada_programada(cast(Any, params), "revision-abuela", dentro_de())
        assert "error" in params.resultado

    async def test_cancelar_la_retira(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar.")
        await cancelar_llamada_programada(cast(Any, params), params.resultado["id"])
        assert params.resultado["cancelada"] is True
        assert cargar_misiones(tmp_path).pendientes == []

    async def test_cancelar_un_id_desconocido_devuelve_falso(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await cancelar_llamada_programada(cast(Any, params), "agenda-inventada")
        assert params.resultado["cancelada"] is False


class TestListar:
    async def test_lista_las_pendientes(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path, numero_llamada="3046411802")
        await programar_llamada(cast(Any, params), dentro_de(), "Retomar el control.")
        await llamadas_programadas(cast(Any, params))
        assert params.resultado["total"] == 1
        assert params.resultado["llamadas"][0]["encargo"] == "Retomar el control."

    async def test_sin_ninguna_lo_dice_sin_fallar(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path)
        await llamadas_programadas(cast(Any, params))
        assert params.resultado["total"] == 0

    async def test_avisa_de_no_leer_los_identificadores(self, tmp_path: Path) -> None:
        params, _ = recursos_con(tmp_path)
        await llamadas_programadas(cast(Any, params))
        assert "identificadores" in params.resultado["aviso"]
