"""Tests de las herramientas de triaje y resumen de llamada."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pipecat.services.llm_service import FunctionCallParams

from voice_agent.rag.retriever import Pasaje
from voice_agent.resources import AppResources
from voice_agent.tools.evaluacion import finalizar_llamada, registrar_alerta
from voice_agent.traza import TrazaLlamada
from voice_agent_core.config import Settings
from voice_agent_core.rutas import dir_alertas, dir_resumenes


@dataclass
class ParamsFalsos:
    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _params(tmp_path: Path, *, con_traza: bool = True) -> ParamsFalsos:
    traza = TrazaLlamada(tmp_path, id_llamada="llamada-test") if con_traza else None
    recursos = AppResources(
        settings=Settings(_env_file=None, data_dir=tmp_path),  # type: ignore[call-arg]
        retriever=cast(Any, object()),
        traza=traza,
    )
    return ParamsFalsos(app_resources=recursos)


class TestRegistrarAlerta:
    async def test_persiste_la_alerta_con_su_estructura(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params),
            nivel="rojo",
            sintomas="Fiebre de treinta y nueve y pus en la herida.",
            justificacion="La guía de apendicectomía lo lista como signo de alarma.",
        )

        (fichero,) = list(dir_alertas(tmp_path).iterdir())
        datos = json.loads(fichero.read_text())
        assert datos["nivel"] == "rojo"
        assert datos["id_llamada"] == "llamada-test"
        assert "pus" in datos["sintomas"]
        assert params.resultado["registrada"] is True

    async def test_el_resultado_le_dice_al_modelo_que_comunicar(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="rojo", sintomas="x", justificacion="y"
        )
        assert "urgencias" in params.resultado["que_decirle_al_paciente"]

        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="verde", sintomas="x", justificacion="y"
        )
        assert "normal" in params.resultado["que_decirle_al_paciente"]

    async def test_un_nivel_invalido_se_rechaza_sin_escribir(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="morado", sintomas="x", justificacion="y"
        )

        assert "error" in params.resultado
        assert not dir_alertas(tmp_path).exists() or not list(dir_alertas(tmp_path).iterdir())

    async def test_el_nivel_se_normaliza(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel=" Amarillo ", sintomas="x", justificacion="y"
        )
        assert params.resultado["nivel"] == "amarillo"

    async def test_dos_alertas_no_se_pisan(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="verde", sintomas="a", justificacion="b"
        )
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="rojo", sintomas="c", justificacion="d"
        )
        assert len(list(dir_alertas(tmp_path).iterdir())) == 2


class TestFinalizarLlamada:
    async def test_persiste_los_cinco_campos_y_la_traza(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        recursos: AppResources = params.app_resources
        assert recursos.traza is not None
        recursos.traza.registrar(
            "cuidados de la herida",
            [Pasaje(texto="t", origen="apendicitis/guia.pdf", distancia=0.3)],
        )

        await finalizar_llamada(
            cast(FunctionCallParams, params),
            paciente_y_procedimiento="Nora, apendicectomía hace tres días.",
            sintomas="Dolor leve, sin fiebre.",
            decision="Verde; se le indicaron cuidados en casa.",
            proximos_pasos="Control con su cirujano la próxima semana.",
            referencias="guía de recuperación de apendicectomía",
        )

        (fichero,) = list(dir_resumenes(tmp_path).iterdir())
        datos = json.loads(fichero.read_text())
        assert datos["paciente_y_procedimiento"].startswith("Nora")
        assert datos["decision"].startswith("Verde")
        assert datos["proximos_pasos"]
        assert datos["sintomas"]
        assert datos["referencias"]
        assert datos["documentos_consultados"] == ["apendicitis/guia.pdf"]
        assert params.resultado["guardado"] is True

    async def test_el_color_del_triaje_viene_de_la_alerta_no_del_modelo(
        self, tmp_path: Path
    ) -> None:
        # La gravedad tiene que quedar inequívoca en el registro de la
        # llamada: el campo `nivel` lo copia el sistema de la última alerta
        # registrada, y la redacción libre de `decision` no puede sustituirlo.
        params = _params(tmp_path)
        await registrar_alerta(
            cast(FunctionCallParams, params), nivel="rojo", sintomas="s", justificacion="j"
        )
        await finalizar_llamada(
            cast(FunctionCallParams, params),
            paciente_y_procedimiento="p",
            sintomas="s",
            decision="d",  # ni menciona el color
            proximos_pasos="n",
        )

        (fichero,) = list(dir_resumenes(tmp_path).iterdir())
        assert json.loads(fichero.read_text())["nivel"] == "rojo"

    async def test_sin_alerta_el_nivel_queda_vacio(self, tmp_path: Path) -> None:
        params = _params(tmp_path)
        await finalizar_llamada(
            cast(FunctionCallParams, params),
            paciente_y_procedimiento="p",
            sintomas="s",
            decision="d",
            proximos_pasos="n",
        )
        (fichero,) = list(dir_resumenes(tmp_path).iterdir())
        assert json.loads(fichero.read_text())["nivel"] == ""

    async def test_sin_traza_el_resumen_se_guarda_igual(self, tmp_path: Path) -> None:
        params = _params(tmp_path, con_traza=False)
        await finalizar_llamada(
            cast(FunctionCallParams, params),
            paciente_y_procedimiento="p",
            sintomas="s",
            decision="d",
            proximos_pasos="n",
        )

        (fichero,) = list(dir_resumenes(tmp_path).iterdir())
        datos = json.loads(fichero.read_text())
        assert datos["id_llamada"] == "sin-traza"
        assert datos["documentos_consultados"] == []
