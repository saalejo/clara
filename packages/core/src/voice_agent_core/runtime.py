"""Configuración que el panel gobierna y que no cabe en variables de entorno.

`Settings` es una lista plana de escalares y encaja bien en el entorno. El
prompt, el alma, la lista de servidores MCP y la de hooks no: son estructuras
anidadas y listas de longitud variable. Meterlas en variables de entorno sería
forzar la herramienta, así que viajan en su propio JSON.

Separarlas tiene una segunda ventaja: **los valores por defecto salen de
`prompts.py`**, de modo que un clon recién bajado, sin panel y sin ficheros de
configuración, se comporta exactamente igual que antes de que el panel
existiera. El panel es una capa opcional encima, no un requisito.

Este módulo lo importan los dos lados —el agente para leer, el panel para
escribir—, y esa es la razón de que el contrato no pueda separarse entre ambos.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from voice_agent_core.prompts import MULETILLAS, PROMPT_SISTEMA, SALUDO_INICIAL
from voice_agent_core.rutas import ruta_runtime

#: Tope de espera para un hook que bloquea el turno. Ver `HookConfig.bloqueante`.
TIMEOUT_MAXIMO_BLOQUEANTE = 2.0


class PromptConfig(BaseModel):
    """El prompt del sistema, el alma y los textos fijos que oye el usuario."""

    prompt_sistema: str = Field(
        default=PROMPT_SISTEMA,
        description="Instrucciones base: cómo hablar, cómo usar las herramientas, qué hacer si no sabe algo.",
    )
    alma: str = Field(
        default="",
        description=(
            "Personalidad, tono y límites propios de esta instalación. Se añade "
            "**tal cual al final** del prompt del sistema, sin encabezados ni "
            "reformateo. Se guarda aparte para poder cambiar el carácter del "
            "agente sin tocar las reglas de funcionamiento, y al revés."
        ),
    )
    saludo_inicial: str = Field(
        default=SALUDO_INICIAL,
        description=(
            "Frase fija que se sintetiza al arrancar. Es texto, no una "
            "instrucción al modelo: pedírselo por mensaje de sistema causaba un "
            "bucle de saludos (ver el comentario en prompts.py)."
        ),
    )
    muletillas: dict[str, list[str]] = Field(
        default_factory=lambda: {clave: list(valor) for clave, valor in MULETILLAS.items()},
        description="Frases de espera por categoría: 'consulta' y 'pensando'.",
    )

    @property
    def prompt_sistema_efectivo(self) -> str:
        """El texto que se le manda de verdad al modelo.

        Es el único sitio donde se decide cómo se combinan prompt y alma; en la
        base de datos del panel siguen siendo dos campos separados.
        """
        alma = self.alma.strip()
        if not alma:
            return self.prompt_sistema
        return f"{self.prompt_sistema.rstrip()}\n\n{alma}\n"


class ToolConfig(BaseModel):
    """Una herramienta local y si el modelo llega a verla."""

    nombre: str
    habilitada: bool = True


class TransporteMCP(StrEnum):
    """Cómo se habla con un servidor MCP.

    Attributes:
        STDIO: Se lanza como proceso hijo y se habla por su entrada y salida
            estándar. Es el transporte de los servidores que se instalan en la
            máquina.
        HTTP: HTTP con streaming ("streamable HTTP"), el transporte moderno para
            servidores remotos.
        SSE: Server-Sent Events, el transporte remoto anterior. Se mantiene
            porque aún hay servidores que solo hablan así.
    """

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class MCPServerConfig(BaseModel):
    """Un servidor MCP cuyas herramientas se le ofrecen al modelo."""

    nombre: str = Field(description="Identificador corto; sale en los logs y en el panel.")
    habilitado: bool = False
    transporte: TransporteMCP = TransporteMCP.STDIO

    comando: str | None = Field(default=None, description="Ejecutable a lanzar. Solo para stdio.")
    argumentos: list[str] = Field(default_factory=list)
    entorno: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Variables para el proceso hijo. Admite `${VAR}`, que el **agente** "
            "resuelve contra su propio entorno al arrancar. Es la forma de dar "
            "una clave de API a un servidor MCP sin que el panel llegue a verla."
        ),
    )

    url: str | None = Field(default=None, description="Extremo del servidor. Solo para http y sse.")
    cabeceras: dict[str, str] = Field(
        default_factory=dict, description="Admite `${VAR}` igual que `entorno`."
    )

    herramientas_permitidas: list[str] = Field(
        default_factory=list,
        description="Lista blanca de nombres. Vacía significa 'todas las que ofrezca'.",
    )
    timeout_secs: float = Field(
        default=20.0,
        gt=0,
        description=(
            "Tope para conectar y listar herramientas. Se aplica de verdad: un "
            "servidor que no responde no puede impedir que el agente arranque."
        ),
    )

    @model_validator(mode="after")
    def _coherencia_del_transporte(self) -> MCPServerConfig:
        """Exige los campos que cada transporte necesita.

        Sin esto, un servidor mal configurado se descubre en el arranque del
        agente —40 segundos más tarde y en otro contenedor— en vez de al
        guardarlo en el panel.
        """
        if self.transporte is TransporteMCP.STDIO:
            if not (self.comando or "").strip():
                raise ValueError(
                    f"El servidor MCP '{self.nombre}' usa stdio y necesita un comando."
                )
        elif not (self.url or "").strip():
            raise ValueError(
                f"El servidor MCP '{self.nombre}' usa {self.transporte} y necesita una url."
            )
        return self


class EventoHook(StrEnum):
    """Momento de la conversación en el que se dispara un hook.

    Los nombres describen lo que ha pasado, no el frame de Pipecat que lo
    representa: el panel los enseña tal cual y quien los configura no tiene por
    qué saber cómo se llaman las cosas por dentro.

    Attributes:
        TRANSCRIPCION_LISTA: Hay transcripción del usuario y **todavía no ha
            entrado en el historial**. Es el único punto donde reescribirla o
            descartarla surte efecto.
        USUARIO_TERMINO: El usuario acaba de callar.
        RESPUESTA_TEXTO: Llega un fragmento de la respuesta del modelo, camino
            del sintetizador.
        LLAMADA_HERRAMIENTA: El modelo ha pedido ejecutar una herramienta.
        RESULTADO_HERRAMIENTA: La herramienta ha devuelto su resultado.
        ERROR: Algo ha fallado dentro del pipeline.
    """

    TRANSCRIPCION_LISTA = "transcripcion_lista"
    USUARIO_TERMINO = "usuario_termino"
    RESPUESTA_TEXTO = "respuesta_texto"
    LLAMADA_HERRAMIENTA = "llamada_herramienta"
    RESULTADO_HERRAMIENTA = "resultado_herramienta"
    ERROR = "error"


class AccionHook(StrEnum):
    """Qué hace un hook cuando se dispara.

    Attributes:
        EJECUTAR_COMANDO: Lanza un proceso y le pasa el contexto del evento por
            la entrada estándar en JSON.
        REESCRIBIR: Aplica una sustitución por expresión regular al texto.
        VETAR: Descarta el texto si casa con la expresión regular.
    """

    EJECUTAR_COMANDO = "ejecutar_comando"
    REESCRIBIR = "reescribir"
    VETAR = "vetar"


#: Eventos cuyo frame lleva texto y, por tanto, admiten reescritura o veto. El
#: resto solo puede disparar comandos: descartar un frame de control cuelga el
#: pipeline entero, así que no se ofrece siquiera como opción.
EVENTOS_CON_TEXTO = frozenset({EventoHook.TRANSCRIPCION_LISTA, EventoHook.RESPUESTA_TEXTO})


class HookConfig(BaseModel):
    """Una regla que se dispara en un punto de la conversación."""

    nombre: str
    habilitado: bool = Field(
        default=False,
        description="Se crea desactivado a propósito: un hook mal escrito no debe estrenarse solo.",
    )
    orden: int = Field(default=100, description="Menor va antes cuando varios comparten evento.")
    evento: EventoHook
    accion: AccionHook

    comando: list[str] = Field(
        default_factory=list,
        description=(
            "Ejecutable y argumentos, ya troceados. **Nunca se pasa por una "
            "shell**: sin comodines, sin tuberías y sin sorpresas de comillas."
        ),
    )
    entorno: dict[str, str] = Field(default_factory=dict)
    timeout_secs: float = Field(default=5.0, gt=0)
    bloqueante: bool = Field(
        default=False,
        description=(
            "Si el turno espera a que el comando termine. Por defecto no: un "
            "hook de cinco segundos en modo bloqueante añade cinco segundos a "
            f"**cada** turno. Bloqueando, el tope son {TIMEOUT_MAXIMO_BLOQUEANTE} s."
        ),
    )

    patron: str = Field(default="", description="Expresión regular de Python.")
    reemplazo: str = Field(default="", description="Sustitución, con \\1 para los grupos.")

    @model_validator(mode="after")
    def _coherencia_de_la_accion(self) -> HookConfig:
        """Comprueba que la acción tiene lo que necesita y que se puede aplicar."""
        if self.accion is AccionHook.EJECUTAR_COMANDO:
            if not self.comando:
                raise ValueError(
                    f"El hook '{self.nombre}' ejecuta un comando pero no tiene ninguno."
                )
            if self.bloqueante and self.timeout_secs > TIMEOUT_MAXIMO_BLOQUEANTE:
                raise ValueError(
                    f"El hook '{self.nombre}' es bloqueante, así que su timeout no puede pasar de "
                    f"{TIMEOUT_MAXIMO_BLOQUEANTE} s: se sumaría a cada turno de la conversación."
                )
            return self

        if not self.patron.strip():
            raise ValueError(
                f"El hook '{self.nombre}' es de tipo {self.accion} y necesita un patrón."
            )
        if self.evento not in EVENTOS_CON_TEXTO:
            legibles = ", ".join(sorted(e.value for e in EVENTOS_CON_TEXTO))
            raise ValueError(
                f"El hook '{self.nombre}' quiere {self.accion} en '{self.evento}', pero ese evento "
                f"no lleva texto. Reescribir o vetar solo tiene sentido en: {legibles}."
            )
        return self


class RuntimeConfig(BaseModel):
    """Todo lo que el panel gobierna y no es un campo de `Settings`."""

    version: int = 1
    generado_en: datetime | None = None
    perfil: str = Field(
        default="",
        description=(
            "Nombre del perfil del panel que generó esta configuración. Solo "
            "informativo: sale en el banner de arranque para saber qué hay "
            "puesto. El agente no cambia de comportamiento por él."
        ),
    )
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    herramientas: list[ToolConfig] = Field(default_factory=list)
    mcp: list[MCPServerConfig] = Field(default_factory=list)
    hooks: list[HookConfig] = Field(default_factory=list)

    @property
    def herramientas_desactivadas(self) -> frozenset[str]:
        """Nombres de herramientas locales que el modelo no debe llegar a ver."""
        return frozenset(h.nombre for h in self.herramientas if not h.habilitada)

    @property
    def servidores_mcp_activos(self) -> list[MCPServerConfig]:
        """Servidores MCP que hay que intentar conectar al arrancar."""
        return [s for s in self.mcp if s.habilitado]

    def hooks_de(self, evento: EventoHook) -> list[HookConfig]:
        """Hooks activos de un evento, ya ordenados."""
        return sorted(
            (h for h in self.hooks if h.habilitado and h.evento is evento),
            key=lambda h: (h.orden, h.nombre),
        )

    @property
    def hay_hooks(self) -> bool:
        """Indica si merece la pena insertar procesadores de hooks en el pipeline."""
        return any(h.habilitado for h in self.hooks)


def cargar_runtime(data_dir: Path) -> RuntimeConfig:
    """Lee la configuración del panel, o devuelve la de por defecto.

    Nunca lanza. Un JSON corrupto o un modelo que no valida degradan a la
    configuración por defecto con un aviso en el log: es preferible un agente
    que arranca con el prompt de fábrica a una placa muda porque alguien guardó
    algo raro desde el panel.

    Args:
        data_dir: La raíz de datos, normalmente `Settings.data_dir`.

    Returns:
        La configuración leída, o una por defecto.
    """
    ruta = ruta_runtime(data_dir)
    if not ruta.is_file():
        logger.debug(f"Sin configuración del panel en {ruta}; se usan los valores por defecto.")
        return RuntimeConfig()
    try:
        return RuntimeConfig.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(
            f"No se pudo leer la configuración del panel en {ruta}: {e}. "
            "Se arranca con los valores por defecto."
        )
        return RuntimeConfig()
