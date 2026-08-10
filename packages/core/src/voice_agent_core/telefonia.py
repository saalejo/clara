"""Modelos compartidos entre el puente de telefonía, el agente y el panel.

Este módulo es el **contrato del cable**: lo que viaja por el socket unix que
une el puente (`voice_agent_telefonia`, nativo) con el agente (`voice_agent`,
dentro del contenedor). Vive en `core` y no en ninguno de los dos porque los
dos lo necesitan y ninguno puede importar al otro: el puente no puede cargar
Pipecat y el agente no puede cargar dbus-fast.

Solo hay pydantic aquí. Es deliberado y lo vigila `tests/test_core_liviano.py`:
importar `voice_agent_core` no debe cargar nada pesado.

## Los estados de una llamada

Los siete valores de `EstadoLlamada` son una traducción uno a uno de la
propiedad `State` de `org.ofono.VoiceCall`. Se traducen en vez de usarse tal
cual por dos motivos: el resto del proyecto habla español, y así el contrato del
socket no queda amarrado a los nombres de oFono si algún día se cambia de pila.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

#: Lo que se dice de una llamada cuyo número viene oculto. oFono entrega la
#: cadena "withheld" en `LineIdentification` cuando el que llama ha ocultado su
#: número; sin esto, el agente diría "te llama withheld".
NUMERO_OCULTO = "número oculto"

#: Lo que se dice de una llamada de una app de mensajería (WhatsApp y demás).
LLAMADA_DE_APP = "alguien por una aplicación"

#: Identificadores de relleno que Android entrega por HFP cuando la llamada
#: **no es del operador**.
#:
#: Las apps de VoIP se integran en Android como `ConnectionService`
#: autogestionado, y por eso sus llamadas sí llegan al manos libres y se pueden
#: contestar y colgar. Lo que no llega es el número: el identificador de llamada
#: de HFP solo sabe de números de teléfono, así que Android rellena el hueco.
#:
#: Medido en esta placa con una llamada de WhatsApp real: `LineIdentification`
#: llegó como `10000000`. Sin traducirlo, el agente anunciaba «te llama uno cero
#: cero cero cero cero cero cero», que no le dice nada a nadie. Es además la
#: pista que reveló que las llamadas de prueba anteriores eran de WhatsApp y no
#: del operador.
RELLENOS_SIN_IDENTIFICAR: frozenset[str] = frozenset({"10000000"})


class EstadoLlamada(StrEnum):
    """Situación de una llamada, traducida de `org.ofono.VoiceCall.State`.

    Attributes:
        ENTRANTE: Está sonando y nadie la ha cogido (oFono: `incoming`).
        EN_ESPERA: Entra mientras ya hay otra conversación (oFono: `waiting`).
        MARCANDO: La hemos iniciado y aún no suena al otro lado (`dialing`).
        SONANDO: La hemos iniciado y ya suena al otro lado (`alerting`).
        EN_CURSO: Conversación en marcha (oFono: `active`).
        RETENIDA: En espera, puesta en pausa por nosotros (oFono: `held`).
        TERMINADA: Ha colgado alguien (oFono: `disconnected`).
    """

    ENTRANTE = "entrante"
    EN_ESPERA = "en_espera"
    MARCANDO = "marcando"
    SONANDO = "sonando"
    EN_CURSO = "en_curso"
    RETENIDA = "retenida"
    TERMINADA = "terminada"


#: Traducción de los valores de `State` de oFono. Se deja explícita, y no como
#: una transformación de la cadena, para que un valor nuevo o inesperado de
#: oFono se detecte en vez de colarse a medio traducir.
ESTADOS_OFONO: dict[str, EstadoLlamada] = {
    "incoming": EstadoLlamada.ENTRANTE,
    "waiting": EstadoLlamada.EN_ESPERA,
    "dialing": EstadoLlamada.MARCANDO,
    "alerting": EstadoLlamada.SONANDO,
    "active": EstadoLlamada.EN_CURSO,
    "held": EstadoLlamada.RETENIDA,
    "disconnected": EstadoLlamada.TERMINADA,
}

#: Estados en los que la llamada todavía existe y se puede actuar sobre ella.
ESTADOS_VIVOS: frozenset[EstadoLlamada] = frozenset(
    {
        EstadoLlamada.ENTRANTE,
        EstadoLlamada.EN_ESPERA,
        EstadoLlamada.MARCANDO,
        EstadoLlamada.SONANDO,
        EstadoLlamada.EN_CURSO,
        EstadoLlamada.RETENIDA,
    }
)

#: Estados en los que "contestar" tiene sentido.
#:
#: **`SONANDO` no está aquí, y no es un olvido.** Es tentador añadirlo al ver que
#: las llamadas de app nacen en `alerting`, pero sería un arreglo falso por dos
#: motivos independientes, y cada uno basta:
#:
#: 1. **oFono no lo permitiría.** Su `src/voicecall.c` abre el manejador de
#:    `Answer()` con `if (call->status != CALL_STATUS_INCOMING) return
#:    __ofono_error_failed(msg);`. `HoldAndAnswer` exige a su vez una llamada en
#:    `waiting`, y `org.ofono.Handsfree` solo expone `GetProperties`,
#:    `SetProperty` y `RequestPhoneNumber`. Mientras oFono sea el dueño del canal
#:    RFCOMM no hay otra vía de mandar el `ATA`.
#: 2. **Y este móvil no las ofrece nunca como `incoming`.** Medido en tres
#:    llamadas de WhatsApp reales: aparecen como `dialing` o como `alerting`
#:    —alterna entre las dos— y a los ~140 ms pasan a `active`. Ninguno de esos
#:    tres estados es `incoming`, así que no hay instante en el que `Answer()`
#:    fuera legal. El móvil describe una llamada que ENTRA con los indicadores
#:    de una que SALE.
#:
#: La consecuencia hay que decirla entera: **una llamada de app no se puede
#: descolgar desde la placa**, ni a mano ni con el autocontestar. Y su `active`
#: tampoco significa que esté descolgada; ver `Llamada.es_de_app`.
ESTADOS_CONTESTABLES: frozenset[EstadoLlamada] = frozenset(
    {EstadoLlamada.ENTRANTE, EstadoLlamada.EN_ESPERA}
)


class Llamada(BaseModel):
    """Una llamada, tal y como la ve el agente."""

    id: str = Field(description="Cola de la ruta D-Bus de oFono, p. ej. 'voicecall01'.")
    estado: EstadoLlamada
    numero: str = Field(default="", description="Número del otro extremo, si se conoce.")
    nombre_red: str = Field(
        default="",
        description="Nombre que manda la red (CNAP). Casi siempre viene vacío en España y Colombia.",
    )
    nombre_agenda: str | None = Field(
        default=None,
        description="Nombre resuelto contra la agenda local a partir del número.",
    )
    entrante: bool
    inicio: datetime | None = None

    @property
    def quien(self) -> str:
        """Devuelve la mejor forma de referirse al otro extremo, para decirla en voz alta.

        El orden no es arbitrario: la agenda gana a la red porque es la que
        conoce el usuario ("Mamá" y no "María Fernanda Ruiz"), y el número es el
        último recurso porque escuchar nueve dígitos seguidos no le dice nada a
        nadie.
        """
        if self.nombre_agenda:
            return self.nombre_agenda
        if self.nombre_red:
            return self.nombre_red
        if self.numero in RELLENOS_SIN_IDENTIFICAR:
            return LLAMADA_DE_APP
        if self.numero and self.numero != NUMERO_OCULTO:
            return self.numero
        return NUMERO_OCULTO

    @property
    def viva(self) -> bool:
        """Indica si la llamada sigue existiendo."""
        return self.estado in ESTADOS_VIVOS

    @property
    def es_de_app(self) -> bool:
        """Indica si viene de una aplicación de mensajería y no del operador.

        Lo delata el identificador de relleno; ver `RELLENOS_SIN_IDENTIFICAR`.

        No es un detalle cosmético, y por eso está en el modelo y no escondido en
        quien lo use: **de una llamada de app no hay que fiarse del estado.**
        Medido con dos llamadas reales, el móvil las declara `active` por HFP a
        los ~140 ms mientras la aplicación sigue timbrando y nadie las ha
        cogido. Así que `EN_CURSO` en una llamada de app no significa que haya
        nadie al otro lado.
        """
        return self.numero in RELLENOS_SIN_IDENTIFICAR


class NumeroContacto(BaseModel):
    """Un número dentro de la ficha de un contacto."""

    tipo: str = Field(default="otro", description="movil | casa | trabajo | otro")
    numero: str


class Contacto(BaseModel):
    """Una ficha de la agenda del móvil."""

    nombre: str
    numeros: list[NumeroContacto] = Field(default_factory=list)

    @property
    def numero_preferido(self) -> str:
        """Devuelve el número al que llamar cuando hay varios.

        Se prefiere el móvil: es al que la gente espera que la llamen, y es el
        único que casi seguro está encendido. Si no hay móvil, vale el primero.
        """
        for numero in self.numeros:
            if numero.tipo == "movil":
                return numero.numero
        return self.numeros[0].numero if self.numeros else ""


class Coincidencia(BaseModel):
    """Un contacto encontrado al buscar, con lo bien que encaja con lo buscado."""

    contacto: Contacto
    puntuacion: int = Field(ge=0, le=100)


class EstadoTelefonia(BaseModel):
    """Foto completa de la telefonía, para el agente y para el panel."""

    disponible: bool = Field(description="Hay un móvil conectado y con módem HFP listo.")
    adaptador_encendido: bool = False
    telefono_direccion: str | None = None
    telefono_nombre: str | None = None
    telefono_conectado: bool = False
    modem_listo: bool = False
    llamadas: list[Llamada] = Field(default_factory=list)
    contactos: int = 0
    contactos_actualizados: datetime | None = None
    autocontestar: bool = Field(
        default=False,
        description=(
            "Si el puente descuelga solo las llamadas entrantes. OJO con lo que "
            "significa hoy: descolgar, no conversar. El audio de la llamada sigue "
            "yendo por el altavoz del móvil, no por la placa — ver la fase 2 de "
            "docs/telefonia.md, que está sin empezar."
        ),
    )
    autocontestar_segundos: float = Field(
        default=2.0,
        description="Margen antes de descolgar solo, para poder rechazar a mano.",
    )
    detalle: str = Field(
        default="",
        description=(
            "Una frase en español ya redactada, lista para que el modelo la diga tal cual. "
            "Sigue el mismo patrón que el campo `descripcion` de obtener_fecha_hora: "
            "ahorrarle al LLM el trabajo de redactar evita que se invente matices."
        ),
    )


class TipoEvento(StrEnum):
    """Clases de evento que el puente emite por su canal SSE.

    **Añadir uno es un cambio entre versiones, no un cambio local.** El puente
    corre nativo y se actualiza al instante; el agente vive en una imagen que
    tarda veinte minutos en reconstruirse. Así que durante ese rato el puente
    emite un `tipo` que el enum del agente no conoce, y
    `EventoTelefonia.model_validate` falla.

    Sobrevive, y conviene saber por qué: `ValidationError` es un `ValueError`, y
    `ClienteTelefonia.eventos` captura `(ValueError, TypeError)`, lo registra
    como «Evento de telefonía ilegible» y sigue leyendo el flujo. Es decir, el
    agente se pierde el evento nuevo pero no se rompe ni deja de recibir los que
    sí conoce. Comprobado leyendo `src/voice_agent/telefonia.py`.
    """

    LLAMADA_ENTRANTE = "llamada_entrante"
    LLAMADA_CONTESTADA = "llamada_contestada"
    LLAMADA_TERMINADA = "llamada_terminada"
    LLAMADA_SALIENTE = "llamada_saliente"
    TELEFONO_CONECTADO = "telefono_conectado"
    TELEFONO_DESCONECTADO = "telefono_desconectado"
    AGENDA_ACTUALIZADA = "agenda_actualizada"
    AUTOCONTESTAR_CAMBIADO = "autocontestar_cambiado"


class EventoTelefonia(BaseModel):
    """Algo que ha pasado en el teléfono y que el agente puede querer anunciar."""

    tipo: TipoEvento
    momento: datetime
    llamada: Llamada | None = None
    motivo: str = ""


class ResultadoSincronizacion(BaseModel):
    """Cómo ha ido una descarga de la agenda por PBAP."""

    ok: bool
    contactos: int = 0
    segundos: float = 0.0
    detalle: str = ""


__all__ = [
    "ESTADOS_CONTESTABLES",
    "ESTADOS_OFONO",
    "ESTADOS_VIVOS",
    "NUMERO_OCULTO",
    "Coincidencia",
    "Contacto",
    "EstadoLlamada",
    "EstadoTelefonia",
    "EventoTelefonia",
    "Llamada",
    "NumeroContacto",
    "ResultadoSincronizacion",
    "TipoEvento",
]
