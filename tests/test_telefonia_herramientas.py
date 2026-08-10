"""Las siete herramientas de teléfono, con un puente falso.

El test que importa es `test_sin_confirmar_no_marca`. Todo lo demás es higiene;
ese es el que impide que una regresión convierta al agente en algo que llama a
gente sin permiso.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pipecat.adapters.schemas.direct_function import DirectFunctionWrapper
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources
from voice_agent.telefonia import ErrorTelefonia
from voice_agent.tools.telefono import (
    HERRAMIENTAS_TELEFONIA,
    buscar_contacto,
    colgar_llamada,
    contestar_llamada,
    estado_del_telefono,
    llamar_a_contacto,
    llamar_a_numero,
    marcar_tonos,
)
from voice_agent_core.config import Settings
from voice_agent_core.telefonia import (
    Coincidencia,
    Contacto,
    EstadoLlamada,
    EstadoTelefonia,
    Llamada,
    NumeroContacto,
)


def contacto(nombre: str, numero: str) -> Contacto:
    return Contacto(nombre=nombre, numeros=[NumeroContacto(tipo="movil", numero=numero)])


@dataclass
class ClienteFalso:
    """Puente de mentira: registra lo que le piden y no toca nada."""

    coincidencias: list[Coincidencia] = field(default_factory=list)
    ambiguo: bool = False
    pregunta: str = ""
    falla_con: ErrorTelefonia | None = None

    marcados: list[str] = field(default_factory=list)
    contestadas: int = 0
    colgadas: int = 0
    tonos_enviados: list[str] = field(default_factory=list)

    def _quizas_fallar(self) -> None:
        if self.falla_con is not None:
            raise self.falla_con

    async def estado(self) -> EstadoTelefonia:
        self._quizas_fallar()
        return EstadoTelefonia(
            disponible=True,
            telefono_conectado=True,
            telefono_nombre="Móvil de prueba",
            contactos=3,
            contactos_actualizados=datetime.now(UTC),
            detalle="El teléfono Móvil de prueba está conectado y no hay ninguna llamada.",
        )

    async def buscar_contactos(self, nombre: str, limite: int = 5) -> dict[str, Any]:
        self._quizas_fallar()
        return {
            "coincidencias": self.coincidencias,
            "ambiguo": self.ambiguo,
            "pregunta": self.pregunta,
        }

    async def marcar(self, numero: str) -> Llamada:
        self._quizas_fallar()
        self.marcados.append(numero)
        return Llamada(
            id="voicecall01", estado=EstadoLlamada.MARCANDO, numero=numero, entrante=False
        )

    async def contestar(self, id_llamada: str | None = None) -> Llamada:
        self._quizas_fallar()
        self.contestadas += 1
        return Llamada(
            id="voicecall01",
            estado=EstadoLlamada.EN_CURSO,
            numero="+573001111111",
            nombre_agenda="Ana Pérez",
            entrante=True,
        )

    async def colgar(self, id_llamada: str | None = None) -> None:
        self._quizas_fallar()
        self.colgadas += 1

    async def tonos(self, tonos: str) -> None:
        self._quizas_fallar()
        self.tonos_enviados.append(tonos)


@dataclass
class ParamsFalsos:
    app_resources: Any
    resultado: Any = None

    async def result_callback(self, resultado: Any) -> None:
        self.resultado = resultado


def _params(cliente: ClienteFalso | None) -> ParamsFalsos:
    recursos = AppResources(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        retriever=cast(Any, object()),
        telefonia=cast(Any, cliente),
    )
    return ParamsFalsos(app_resources=recursos)


def p(params: ParamsFalsos) -> FunctionCallParams:
    return cast(FunctionCallParams, params)


# --- Esquemas ---------------------------------------------------------------


class TestEsquemas:
    def test_los_nombres_expuestos_al_modelo(self) -> None:
        nombres = {
            DirectFunctionWrapper(f).to_function_schema().to_default_dict()["name"]
            for f in HERRAMIENTAS_TELEFONIA
        }
        assert nombres == {
            "estado_del_telefono",
            "buscar_contacto",
            "llamar_a_contacto",
            "llamar_a_numero",
            "contestar_llamada",
            "colgar_llamada",
            "marcar_tonos",
        }

    def test_todas_tienen_descripcion(self) -> None:
        for funcion in HERRAMIENTAS_TELEFONIA:
            esquema = DirectFunctionWrapper(funcion).to_function_schema().to_default_dict()
            assert esquema["description"].strip(), f"{funcion.__name__} sin descripción"

    @pytest.mark.parametrize("funcion", [llamar_a_contacto, llamar_a_numero])
    def test_las_que_marcan_exigen_confirmado(self, funcion: Any) -> None:
        """El pestillo tiene que ser un argumento OBLIGATORIO del esquema: si
        fuera opcional, el modelo lo omitiría y el valor por defecto decidiría
        por él."""
        esquema = DirectFunctionWrapper(funcion).to_function_schema().to_default_dict()
        assert "confirmado" in esquema["parameters"]["required"]
        assert esquema["parameters"]["properties"]["confirmado"]["type"] == "boolean"


# --- El pestillo ------------------------------------------------------------


class TestPestilloDeConfirmacion:
    async def test_sin_confirmar_no_marca(self) -> None:
        """EL test. Si esto se rompe, el agente llama a gente sin permiso."""
        cliente = ClienteFalso(
            coincidencias=[
                Coincidencia(contacto=contacto("Ana Pérez", "+573001111111"), puntuacion=100)
            ]
        )
        params = _params(cliente)
        await llamar_a_contacto(p(params), nombre="Ana Pérez", confirmado=False)

        assert cliente.marcados == []
        assert params.resultado["estado"] == "pendiente_de_confirmar"
        assert params.resultado["a_quien"] == "Ana Pérez"

    async def test_confirmado_si_marca(self) -> None:
        cliente = ClienteFalso(
            coincidencias=[
                Coincidencia(contacto=contacto("Ana Pérez", "+573001111111"), puntuacion=100)
            ]
        )
        params = _params(cliente)
        await llamar_a_contacto(p(params), nombre="Ana Pérez", confirmado=True)

        assert cliente.marcados == ["+573001111111"]
        assert params.resultado["estado"] == "llamando"

    async def test_un_numero_dictado_tambien_lleva_pestillo(self) -> None:
        cliente = ClienteFalso()
        params = _params(cliente)
        await llamar_a_numero(p(params), numero="300 123 45 67", confirmado=False)

        assert cliente.marcados == []
        assert params.resultado["estado"] == "pendiente_de_confirmar"


# --- Desambiguación ---------------------------------------------------------


class TestAmbiguedad:
    async def test_dos_anas_no_marcan_ni_confirmadas(self) -> None:
        """Aunque el modelo insista con confirmado=True, si hay duda no se
        marca: elegir entre dos personas no es cosa suya."""
        cliente = ClienteFalso(
            coincidencias=[
                Coincidencia(contacto=contacto("Ana Pérez", "+571"), puntuacion=85),
                Coincidencia(contacto=contacto("Ana Gómez", "+572"), puntuacion=85),
            ],
            ambiguo=True,
            pregunta="¿Ana Pérez o Ana Gómez?",
        )
        params = _params(cliente)
        await llamar_a_contacto(p(params), nombre="Ana", confirmado=True)

        assert cliente.marcados == []
        assert params.resultado["estado"] == "ambiguo"
        assert params.resultado["pregunta"] == "¿Ana Pérez o Ana Gómez?"

    async def test_desconocido_no_marca(self) -> None:
        cliente = ClienteFalso(coincidencias=[])
        params = _params(cliente)
        await llamar_a_contacto(p(params), nombre="Ptolomeo", confirmado=True)

        assert cliente.marcados == []
        assert params.resultado["estado"] == "no_encontrado"

    async def test_un_numero_absurdo_no_marca(self) -> None:
        cliente = ClienteFalso()
        params = _params(cliente)
        await llamar_a_numero(p(params), numero="hola", confirmado=True)

        assert cliente.marcados == []
        assert params.resultado["estado"] == "numero_invalido"


# --- El resto ---------------------------------------------------------------


class TestOtrasHerramientas:
    async def test_estado_devuelve_una_frase_para_decir(self) -> None:
        params = _params(ClienteFalso())
        await estado_del_telefono(p(params))
        assert params.resultado["conectado"] is True
        assert params.resultado["descripcion"].endswith(".")

    async def test_buscar_contacto_encuentra(self) -> None:
        cliente = ClienteFalso(
            coincidencias=[Coincidencia(contacto=contacto("Mamá", "3006666666"), puntuacion=100)]
        )
        params = _params(cliente)
        await buscar_contacto(p(params), nombre="mama")
        assert params.resultado["encontrado"]
        assert params.resultado["contactos"][0]["nombre"] == "Mamá"

    async def test_buscar_contacto_sin_resultados(self) -> None:
        params = _params(ClienteFalso(coincidencias=[]))
        await buscar_contacto(p(params), nombre="nadie")
        assert params.resultado["encontrado"] is False

    async def test_contestar(self) -> None:
        cliente = ClienteFalso()
        params = _params(cliente)
        await contestar_llamada(p(params))
        assert cliente.contestadas == 1
        assert params.resultado["quien"] == "Ana Pérez"

    async def test_colgar(self) -> None:
        cliente = ClienteFalso()
        params = _params(cliente)
        await colgar_llamada(p(params))
        assert cliente.colgadas == 1

    async def test_tonos(self) -> None:
        cliente = ClienteFalso()
        params = _params(cliente)
        await marcar_tonos(p(params), tonos="123#")
        assert cliente.tonos_enviados == ["123#"]


# --- Los fallos son datos, no excepciones -----------------------------------


class TestLosFallosNoSeEscapan:
    @pytest.mark.parametrize(
        ("funcion", "kwargs"),
        [
            (estado_del_telefono, {}),
            (buscar_contacto, {"nombre": "ana"}),
            (llamar_a_contacto, {"nombre": "ana", "confirmado": True}),
            (llamar_a_numero, {"numero": "3001234567", "confirmado": True}),
            (contestar_llamada, {}),
            (colgar_llamada, {}),
            (marcar_tonos, {"tonos": "1"}),
        ],
    )
    async def test_con_el_puente_caido_devuelven_error(
        self, funcion: Any, kwargs: dict[str, Any]
    ) -> None:
        """Ninguna herramienta puede dejar escapar una excepción: un fallo es
        un dato que el modelo tiene que poder contarle a la persona."""
        cliente = ClienteFalso(
            falla_con=ErrorTelefonia("el puente no está", sugerencia="Dile que no hay teléfono.")
        )
        params = _params(cliente)
        await funcion(p(params), **kwargs)

        assert "error" in params.resultado
        assert params.resultado["sugerencia"]

    @pytest.mark.parametrize(
        ("funcion", "kwargs"),
        [
            (estado_del_telefono, {}),
            (contestar_llamada, {}),
            (colgar_llamada, {}),
        ],
    )
    async def test_sin_telefonia_configurada_tampoco_revientan(
        self, funcion: Any, kwargs: dict[str, Any]
    ) -> None:
        """Si alguien desactiva la telefonía pero el modelo llama igual."""
        params = _params(None)
        await funcion(p(params), **kwargs)
        assert "error" in params.resultado


def test_las_herramientas_de_telefono_no_estan_en_el_registro_principal() -> None:
    """La separación es lo que mantiene aditivo todo el cambio: sin puente, el
    catálogo que ve el modelo es exactamente el de antes."""
    from voice_agent.tools import HERRAMIENTAS

    assert len(HERRAMIENTAS) == 4
    nombres = {f.__name__ for f in HERRAMIENTAS_TELEFONIA}
    assert nombres.isdisjoint({getattr(h, "__name__", "") for h in HERRAMIENTAS})


def test_la_ruta_del_socket_se_deriva_de_data_dir(tmp_path: Path) -> None:
    """Fijarla a mano reproduciría la trampa de DATA_DIR."""
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert settings.telefonia_socket == tmp_path / "run" / "telefonia.sock"
