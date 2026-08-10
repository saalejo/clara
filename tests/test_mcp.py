"""Tests de la conexión con servidores MCP.

El que de verdad importa es `test_un_servidor_que_falla_no_impide_arrancar`: los
servidores se configuran escribiendo un comando o una URL a mano en un
navegador, así que equivocarse es lo normal. Si eso dejara la placa sin agente,
el panel sería peligroso de usar.

Nada de esto toca la red: el cliente de Pipecat se sustituye por dobles.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from mcp import StdioServerParameters
from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from voice_agent import mcp as modulo_mcp
from voice_agent.mcp import cerrar_clientes, construir_parametros, expandir, iniciar_clientes
from voice_agent_core.runtime import MCPServerConfig, RuntimeConfig, TransporteMCP


def _llm() -> Any:
    """Los dobles no llaman al LLM: solo se lo pasan a register_tools."""
    return object()


def _servidor(**campos: Any) -> MCPServerConfig:
    base: dict[str, Any] = {
        "nombre": "prueba",
        "habilitado": True,
        "transporte": TransporteMCP.STDIO,
        "comando": "mi-servidor",
    }
    base.update(campos)
    return MCPServerConfig(**base)


# --- Expansión de ${VARIABLE} ------------------------------------------------


def test_expandir_sustituye_del_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    # Es lo que permite darle una clave a un servidor MCP sin que el panel la vea.
    monkeypatch.setenv("MI_CLAVE", "secreta-de-verdad")
    assert expandir("Bearer ${MI_CLAVE}") == "Bearer secreta-de-verdad"


def test_expandir_deja_lo_que_no_existe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sustituirlo por cadena vacía daría un error de autenticación incomprensible;
    # dejarlo a la vista dice exactamente qué falta.
    monkeypatch.delenv("NO_DEFINIDA", raising=False)
    assert expandir("${NO_DEFINIDA}") == "${NO_DEFINIDA}"


def test_expandir_no_toca_el_texto_normal() -> None:
    assert expandir("sin variables") == "sin variables"


# --- Traducción a parámetros de transporte -----------------------------------


def test_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "abc")
    parametros = construir_parametros(
        _servidor(comando="mcp-fs", argumentos=["--raiz", "/data"], entorno={"T": "${TOKEN}"})
    )
    assert isinstance(parametros, StdioServerParameters)
    assert parametros.command == "mcp-fs"
    assert parametros.args == ["--raiz", "/data"]
    assert parametros.env == {"T": "abc"}


def test_stdio_no_hereda_el_entorno_del_agente() -> None:
    # `env=None` significaría heredarlo, y ahí viven OPENROUTER_API_KEY y
    # DEEPGRAM_API_KEY. Un servidor MCP no tiene por qué recibirlas.
    parametros = construir_parametros(_servidor(entorno={}))
    assert isinstance(parametros, StdioServerParameters)
    assert parametros.env == {}


def test_http() -> None:
    parametros = construir_parametros(
        _servidor(
            transporte=TransporteMCP.HTTP,
            comando=None,
            url="https://ejemplo.test/mcp",
            cabeceras={"Authorization": "Bearer x"},
        )
    )
    assert isinstance(parametros, StreamableHttpParameters)
    assert parametros.url == "https://ejemplo.test/mcp"
    assert parametros.headers == {"Authorization": "Bearer x"}


def test_sse() -> None:
    parametros = construir_parametros(
        _servidor(transporte=TransporteMCP.SSE, comando=None, url="https://ejemplo.test/sse")
    )
    assert isinstance(parametros, SseServerParameters)


# --- Conexión, con dobles ----------------------------------------------------


class ClienteFalso:
    """Doble de `MCPClient` que no habla con nadie."""

    def __init__(
        self,
        *,
        herramientas: list[str] | None = None,
        falla_en: str | None = None,
        tarda: float = 0.0,
    ) -> None:
        self.herramientas = herramientas or []
        self.falla_en = falla_en
        self.tarda = tarda
        self.cerrado = False

    async def start(self) -> None:
        if self.falla_en == "start":
            raise RuntimeError("no encuentro el comando")
        if self.tarda:
            await asyncio.sleep(self.tarda)

    async def register_tools(self, llm: Any) -> ToolsSchema:
        if self.falla_en == "register":
            raise RuntimeError("el servidor contestó cualquier cosa")
        return ToolsSchema(
            standard_tools=[
                FunctionSchema(name=n, description=f"herramienta {n}", properties={}, required=[])
                for n in self.herramientas
            ]
        )

    async def close(self) -> None:
        self.cerrado = True


def _con_cliente(monkeypatch: pytest.MonkeyPatch, cliente: Any) -> list[dict[str, Any]]:
    """Sustituye `MCPClient` por un doble y registra cómo se le construyó."""
    llamadas: list[dict[str, Any]] = []

    def _fabricar(**kwargs: Any) -> Any:
        llamadas.append(kwargs)
        return cliente

    monkeypatch.setattr(modulo_mcp, "MCPClient", _fabricar)
    return llamadas


async def test_un_servidor_bueno_aporta_sus_herramientas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cliente = ClienteFalso(herramientas=["leer_fichero", "escribir_fichero"])
    _con_cliente(monkeypatch, cliente)

    resultado = await iniciar_clientes(RuntimeConfig(mcp=[_servidor()]), llm=_llm())

    assert [e.name for e in resultado.esquemas] == ["leer_fichero", "escribir_fichero"]
    assert list(resultado.clientes) == [cast(Any, cliente)]
    assert resultado.estados[0].conectado is True
    assert resultado.estados[0].herramientas == ["leer_fichero", "escribir_fichero"]


@pytest.mark.parametrize("momento", ["start", "register"])
async def test_un_servidor_que_falla_no_impide_arrancar(
    momento: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # EL test de este módulo. Un comando mal escrito en el panel no puede dejar
    # la placa muda: se anota el fallo y el agente sigue.
    cliente = ClienteFalso(falla_en=momento)
    _con_cliente(monkeypatch, cliente)

    resultado = await iniciar_clientes(RuntimeConfig(mcp=[_servidor()]), llm=_llm())

    assert resultado.esquemas == []
    assert resultado.clientes == []
    assert resultado.estados[0].conectado is False
    assert resultado.estados[0].error
    assert cliente.cerrado, "un cliente que falló al arrancar hay que cerrarlo igual"


async def test_un_transporte_que_se_cancela_solo_no_tumba_el_arranque(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regresión de un fallo real: con un extremo HTTP caído, el transporte de
    # `mcp` cancela su propio ámbito de anyio y sale un `CancelledError`, que es
    # BaseException y se cuela por debajo de `except Exception`. Antes de aislar
    # la conexión en su propia tarea, esto se llevaba por delante el arranque
    # del agente entero.
    class ClienteQueSeCancela(ClienteFalso):
        async def start(self) -> None:
            raise asyncio.CancelledError("Cancelled via cancel scope")

    cliente = ClienteQueSeCancela()
    _con_cliente(monkeypatch, cliente)

    resultado = await iniciar_clientes(RuntimeConfig(mcp=[_servidor()]), llm=_llm())

    assert resultado.clientes == []
    assert resultado.estados[0].conectado is False
    assert "canceló" in resultado.estados[0].error


async def test_una_cancelacion_de_verdad_si_se_propaga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # La contrapartida del test anterior: si están apagando el agente mientras
    # se conecta, eso NO se puede tragar. Aislar la conexión en otra tarea no
    # puede costar la capacidad de que cancelen al agente.
    _con_cliente(monkeypatch, ClienteFalso(tarda=30.0))

    tarea = asyncio.create_task(
        iniciar_clientes(RuntimeConfig(mcp=[_servidor(timeout_secs=30.0)]), _llm())
    )
    await asyncio.sleep(0.05)
    tarea.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tarea


async def test_un_servidor_que_no_responde_se_corta_por_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cliente = ClienteFalso(tarda=30.0)
    _con_cliente(monkeypatch, cliente)

    resultado = await iniciar_clientes(RuntimeConfig(mcp=[_servidor(timeout_secs=0.2)]), llm=_llm())

    assert resultado.clientes == []
    assert "no respondió" in resultado.estados[0].error


async def test_los_servidores_desactivados_ni_se_intentan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _con_cliente(monkeypatch, ClienteFalso())

    resultado = await iniciar_clientes(RuntimeConfig(mcp=[_servidor(habilitado=False)]), llm=_llm())

    assert resultado.estados == []


async def test_uno_roto_no_arrastra_al_bueno(monkeypatch: pytest.MonkeyPatch) -> None:
    bueno = ClienteFalso(herramientas=["sirve"])
    malo = ClienteFalso(falla_en="start")
    clientes = iter([malo, bueno])

    def _fabricar(**kwargs: Any) -> Any:
        return next(clientes)

    monkeypatch.setattr(modulo_mcp, "MCPClient", _fabricar)

    resultado = await iniciar_clientes(
        RuntimeConfig(mcp=[_servidor(nombre="malo"), _servidor(nombre="bueno")]), llm=_llm()
    )

    assert [e.name for e in resultado.esquemas] == ["sirve"]
    assert [(e.nombre, e.conectado) for e in resultado.estados] == [
        ("malo", False),
        ("bueno", True),
    ]


async def test_la_lista_blanca_llega_al_cliente(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas = _con_cliente(monkeypatch, ClienteFalso())

    await iniciar_clientes(
        RuntimeConfig(mcp=[_servidor(herramientas_permitidas=["solo_esta"])]), llm=_llm()
    )

    assert llamadas[0]["tools_filter"] == ["solo_esta"]


async def test_sin_lista_blanca_se_pasa_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pipecat interpreta `None` como "todas"; una lista vacía no valdría.
    llamadas = _con_cliente(monkeypatch, ClienteFalso())

    await iniciar_clientes(RuntimeConfig(mcp=[_servidor()]), llm=_llm())

    assert llamadas[0]["tools_filter"] is None


# --- Cierre ------------------------------------------------------------------


async def test_cerrar_los_cierra_todos() -> None:
    clientes = [ClienteFalso(), ClienteFalso()]
    await cerrar_clientes(cast(Any, clientes))
    assert all(c.cerrado for c in clientes)


async def test_un_cierre_que_falla_no_impide_cerrar_los_demas() -> None:
    class ClienteQueFallaAlCerrar(ClienteFalso):
        async def close(self) -> None:
            raise RuntimeError("el transporte ya estaba roto")

    bueno = ClienteFalso()
    await cerrar_clientes(cast(Any, [ClienteQueFallaAlCerrar(), bueno]))

    assert bueno.cerrado
