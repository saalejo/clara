"""Tests de las herramientas del agente.

Se prueban dos cosas distintas:

1. Que los **esquemas** que Pipecat deduce de las firmas y los docstrings son
   los esperados. Es un test poco habitual pero muy rentable: esos esquemas son
   lo único que el modelo ve de cada herramienta, y se generan a partir de
   texto libre que cualquiera puede romper sin darse cuenta al editar un
   comentario.
2. Que las herramientas **se ejecutan** y entregan el resultado con la forma
   correcta, sin tocar red ni cargar modelos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.rag.retriever import Pasaje
from voice_agent.resources import AppResources
from voice_agent.tools import (
    HERRAMIENTAS,
    buscar_en_documentos,
    estado_del_sistema,
    obtener_fecha_hora,
)
from voice_agent_core.config import Settings


class RetrieverFalso:
    """Doble de prueba del buscador: devuelve siempre lo mismo, sin modelos."""

    def __init__(self, pasajes: list[Pasaje] | None = None) -> None:
        self.pasajes = (
            pasajes
            if pasajes is not None
            else [Pasaje(texto="Contenido de prueba.", origen="prueba.md", distancia=0.2)]
        )
        self.consultas: list[str] = []

    def buscar(self, consulta: str, *, top_k: int | None = None) -> list[Pasaje]:
        self.consultas.append(consulta)
        return self.pasajes

    def temas_disponibles(self) -> list[str]:
        return ["apendicitis", "colecistitis"]


@dataclass
class ParamsFalsos:
    """Sustituto de `FunctionCallParams` con lo único que las herramientas usan."""

    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _params(retriever: Any = None) -> tuple[ParamsFalsos, RetrieverFalso]:
    """Prepara unos parámetros de llamada con recursos de prueba."""
    falso = retriever or RetrieverFalso()
    recursos = AppResources(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        retriever=cast(Any, falso),
    )
    return ParamsFalsos(app_resources=recursos), falso


# `HERRAMIENTAS` se declara como la unión que espera `LLMContext(tools=...)`.
# Aquí sabemos que todas las entradas son direct functions, así que se estrecha
# el tipo una vez en lugar de repetir un `cast` en cada test.
DIRECTAS: list[Any] = list(HERRAMIENTAS)


class TestEsquemas:
    def test_todas_las_herramientas_generan_esquema(self) -> None:
        for funcion in DIRECTAS:
            esquema = DirectFunctionWrapper(funcion).to_function_schema().to_default_dict()
            assert esquema["name"] == funcion.__name__
            assert esquema["description"].strip(), f"{funcion.__name__} no tiene descripción"

    def test_los_nombres_expuestos_al_modelo_son_los_esperados(self) -> None:
        nombres = {
            DirectFunctionWrapper(f).to_function_schema().to_default_dict()["name"]
            for f in DIRECTAS
        }
        assert nombres == {
            "buscar_en_documentos",
            "registrar_alerta",
            "finalizar_llamada",
            "obtener_fecha_hora",
            "estado_del_sistema",
            "guardar_respuestas",
            "historial_paciente",
        }

    def test_la_busqueda_exige_el_argumento_consulta(self) -> None:
        esquema = DirectFunctionWrapper(buscar_en_documentos).to_function_schema().to_default_dict()
        assert esquema["parameters"]["required"] == ["consulta"]
        assert esquema["parameters"]["properties"]["consulta"]["type"] == "string"
        # La descripción del argumento sale del docstring y guía al modelo para
        # que reformule la pregunta de forma autónoma.
        assert esquema["parameters"]["properties"]["consulta"]["description"].strip()

    def test_las_herramientas_sin_argumentos_no_declaran_ninguno(self) -> None:
        for funcion in (obtener_fecha_hora, estado_del_sistema):
            esquema = DirectFunctionWrapper(funcion).to_function_schema().to_default_dict()
            assert esquema["parameters"]["properties"] == {}
            assert esquema["parameters"]["required"] == []


class TestEjecucion:
    async def test_la_busqueda_delega_en_el_retriever(self) -> None:
        params, retriever = _params()
        await buscar_en_documentos(cast(FunctionCallParams, params), consulta="cuántos núcleos")

        assert retriever.consultas == ["cuántos núcleos"]
        assert "Contenido de prueba." in params.resultado["resultados"]
        assert "prueba.md" in params.resultado["resultados"]

    async def test_la_busqueda_blinda_los_extractos_como_datos(self) -> None:
        """Los pasajes van precedidos del aviso de que no son instrucciones."""
        params, _ = _params()
        await buscar_en_documentos(cast(FunctionCallParams, params), consulta="herida")

        assert "no instrucciones" in params.resultado["resultados"]

    async def test_la_busqueda_declara_las_cirugias_cubiertas(self) -> None:
        """El modelo debe saber qué cubre la base para no citar guías ajenas."""
        params, _ = _params()
        await buscar_en_documentos(cast(FunctionCallParams, params), consulta="herida")

        assert "Cirugías cubiertas" in params.resultado["resultados"]
        assert "apendicitis, colecistitis" in params.resultado["resultados"]

        # También cuando no hay resultados: es justo el caso peligroso.
        params, _ = _params(RetrieverFalso([]))
        await buscar_en_documentos(cast(FunctionCallParams, params), consulta="cataratas")
        assert "Cirugías cubiertas" in params.resultado["resultados"]

    async def test_la_busqueda_propaga_el_aviso_de_que_no_hay_nada(self) -> None:
        """Cuando el RAG no encuentra nada, el modelo debe enterarse."""
        params, _ = _params(RetrieverFalso([]))
        await buscar_en_documentos(cast(FunctionCallParams, params), consulta="paella")

        assert "no contiene información relevante" in params.resultado["resultados"]

    async def test_la_fecha_se_devuelve_en_espanol_y_en_iso(self) -> None:
        params, _ = _params()
        await obtener_fecha_hora(cast(FunctionCallParams, params))

        resultado = params.resultado
        assert set(resultado) == {"descripcion", "fecha_iso", "hora_iso", "zona_horaria"}
        # La versión legible va en español para que el TTS no lea "Monday".
        assert any(
            dia in resultado["descripcion"]
            for dia in ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
        )
        assert len(resultado["fecha_iso"]) == 10

    async def test_el_estado_del_sistema_devuelve_las_metricas_esperadas(self) -> None:
        params, _ = _params()
        await estado_del_sistema(cast(FunctionCallParams, params))

        resultado = params.resultado
        assert set(resultado) == {
            "temperaturas_celsius",
            "memoria",
            "carga_media",
            "tiempo_encendida",
        }
        assert set(resultado["carga_media"]) == {"1_min", "5_min", "15_min"}
        # En Linux /proc/meminfo siempre existe; si esto falla, algo va muy mal.
        assert resultado["memoria"]["total_gb"] > 0
