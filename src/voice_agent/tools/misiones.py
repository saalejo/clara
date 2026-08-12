"""La agenda del agente: programar, mover y cancelar llamadas futuras.

Estas herramientas resuelven las dos formas en que una llamada de seguimiento
se queda a medias. La primera es que la persona no pueda hablar ahora ("estoy
en el trabajo, llámame mañana a las cinco"): hasta ahora el agente lo oía y no
podía hacer nada, y la promesa se perdía. La segunda —que nadie conteste— la
resuelve el planificador solo, con los reintentos; aquí lo que se agenda es lo
que alguien pide en voz alta.

Pertenecen a la misma familia que las de `telefono.py`: **actúan sobre el
mundo**. La diferencia es que actúan *más tarde*, y eso cambia dos cosas. A
favor: no hace falta el pestillo `confirmado`, porque programar no despierta a
nadie y se puede deshacer con `cancelar_llamada_programada`. En contra: cuando
el error se note ya no habrá nadie delante para corregirlo, así que la
validación de la fecha es estricta y el resultado vuelve redactado en español
para que el agente lo lea en voz alta y la persona pueda desmentirlo.

Y la regla de forma de siempre: **ninguna herramienta deja escapar una
excepción**. Un fallo es un dato que el modelo tiene que poder contar, no algo
que rompa el turno.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger
from pipecat.adapters.schemas.direct_function import DirectFunction
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.misiones_agente import AlmacenMisiones
from voice_agent.resources import AppResources
from voice_agent.tools.clock import DIAS, MESES
from voice_agent_core.misiones import MisionPuntual, interpretar_cuando

#: Antelación mínima. Cubre dos cosas: que el modelo no agende algo en el
#: pasado, y que nada nazca ya vencido — el planificador mira el reloj cada
#: treinta segundos, y mientras dura una llamada no mira nada.
ADELANTO_MINIMO_SECS = 120.0

#: Tope por arriba. Un modelo que se equivoca de año dejaría si no una llamada
#: latente durante un lustro.
HORIZONTE_DIAS = 365


def _describir(momento: datetime) -> str:
    """Redacta una fecha en español, para que el agente la diga en voz alta."""
    return (
        f"{DIAS[momento.weekday()]} {momento.day} de {MESES[momento.month - 1]} "
        f"a las {momento.hour:02d}:{momento.minute:02d}"
    )


def _resumen(mision: MisionPuntual) -> dict[str, object]:
    """Lo que se le cuenta al modelo de una misión."""
    return {
        "id": mision.id,
        "cuando": _describir(mision.cuando),
        "cuando_iso": mision.cuando.isoformat(timespec="minutes"),
        "a_quien": mision.contacto_nombre or mision.contacto_numero,
        "encargo": mision.mision,
    }


def _almacen(params: FunctionCallParams) -> AlmacenMisiones | None:
    """Saca la agenda de los recursos compartidos, si este agente la tiene."""
    recursos: AppResources = params.app_resources
    return recursos.almacen_misiones


async def _sin_agenda(params: FunctionCallParams) -> None:
    """Responde que aquí no se pueden agendar llamadas."""
    await params.result_callback(
        {
            "error": "Este agente no puede programar llamadas.",
            "sugerencia": "Dile que tome nota por otro medio y no prometas llamar tú.",
        }
    )


async def _resolver_momento(params: FunctionCallParams, cuando: str) -> datetime | None:
    """Interpreta y valida la fecha; si no vale, ya ha respondido al modelo."""
    recursos: AppResources = params.app_resources
    try:
        momento = interpretar_cuando(cuando, recursos.settings.timezone)
    except ValueError:
        logger.warning(f"[herramienta] fecha ininteligible para la agenda: {cuando!r}")
        await params.result_callback(
            {
                "error": f"No entiendo la fecha '{cuando}'.",
                "sugerencia": ("Pregúntale el día y la hora, y escríbela como AAAA-MM-DD HH:MM."),
            }
        )
        return None

    ahora = datetime.now()
    if momento <= ahora + timedelta(seconds=ADELANTO_MINIMO_SECS):
        await params.result_callback(
            {
                "error": f"{_describir(momento)} ya pasó o es demasiado pronto.",
                "sugerencia": (
                    "Solo puedes programar con unos minutos de antelación. Confirma "
                    "en voz alta el día y la hora, y si de verdad es ahora mismo, "
                    "dile que sigues al teléfono."
                ),
            }
        )
        return None
    if momento > ahora + timedelta(days=HORIZONTE_DIAS):
        await params.result_callback(
            {
                "error": f"{_describir(momento)} está demasiado lejos.",
                "sugerencia": "Comprueba el año y vuelve a intentarlo.",
            }
        )
        return None
    return momento


async def programar_llamada(
    params: FunctionCallParams,
    cuando: str,
    encargo: str,
    nombre: str = "",
    numero: str = "",
) -> None:
    """Agenda una llamada para más adelante y la deja en el calendario.

    Úsala cuando la persona te pida que la llames en otro momento, o cuando la
    conversación se quede a medias y haga falta retomarla. Después de usarla,
    dile en voz alta el día y la hora para que pueda corregirte si te
    equivocaste, y **nunca leas el identificador en voz alta**.

    Args:
        cuando: Fecha y hora exactas, en formato `AAAA-MM-DD HH:MM` y en reloj
            de 24 horas. Nunca escribas aquí "mañana" ni "en dos horas":
            calcula tú la fecha absoluta. Si no sabes qué día es hoy, usa antes
            `obtener_fecha_hora`. Y si te han dicho una hora sin aclarar si es
            de la mañana o de la tarde, PREGÚNTALO antes de programar nada.
        encargo: Qué tendrás que hacer en esa llamada, redactado para ti mismo
            y con el contexto necesario, porque cuando suene no recordarás esta
            conversación. Por ejemplo: "retomar el control del día cinco tras
            la cirugía de vesícula; quedó pendiente preguntar por la fiebre".
        nombre: Cómo se llama la persona, para saludarla al llamar.
        numero: El número al que llamar. Déjalo vacío para llamar al mismo
            número desde el que hablas ahora, que es lo normal.
    """
    almacen = _almacen(params)
    if almacen is None:
        await _sin_agenda(params)
        return

    momento = await _resolver_momento(params, cuando)
    if momento is None:
        return

    recursos: AppResources = params.app_resources
    destino = (numero or recursos.numero_llamada).strip()
    if not destino:
        await params.result_callback(
            {
                "error": "No sé a qué número llamar.",
                "sugerencia": (
                    "Búscalo con buscar_contacto o pídeselo a la persona. No te "
                    "inventes un número ni uses uno que creas recordar."
                ),
            }
        )
        return

    if not encargo.strip():
        await params.result_callback(
            {
                "error": "No has dicho qué hay que hacer en esa llamada.",
                "sugerencia": "Escribe el encargo con el contexto que necesitarás.",
            }
        )
        return

    try:
        mision = await almacen.crear(
            cuando=momento,
            mision=encargo.strip(),
            contacto_numero=destino,
            contacto_nombre=nombre.strip(),
        )
    except (OSError, RuntimeError, ValueError) as e:
        logger.error(f"[herramienta] programar_llamada no pudo guardar: {e}")
        await params.result_callback(
            {
                "error": "No se pudo guardar la llamada programada.",
                "sugerencia": "Dile que no ha quedado agendada y que lo intente por otro medio.",
            }
        )
        return

    logger.info(f"[herramienta] programar_llamada({destino}, {momento:%d/%m %H:%M})")
    await params.result_callback(
        {
            "programada": True,
            **_resumen(mision),
            "aviso": "Dile en voz alta el día y la hora. No leas el identificador.",
        }
    )


async def editar_llamada_programada(
    params: FunctionCallParams, id_llamada: str, cuando: str = "", encargo: str = ""
) -> None:
    """Cambia la hora o el encargo de una llamada que ya tenías programada.

    Úsala cuando la persona quiera mover a otro momento una llamada que ya
    habías agendado, o añadir algo a lo que tenías apuntado. Si no sabes el
    identificador, consúltalo antes con `llamadas_programadas`. Solo puedes
    cambiar llamadas que aún no se hayan hecho.

    Args:
        id_llamada: El identificador exacto que te dio `llamadas_programadas` o
            `programar_llamada`, sin cambiarlo.
        cuando: La fecha y hora nuevas, en formato `AAAA-MM-DD HH:MM`. Déjalo
            vacío si solo quieres cambiar el encargo.
        encargo: El texto nuevo del encargo. Déjalo vacío para no tocarlo.
    """
    almacen = _almacen(params)
    if almacen is None:
        await _sin_agenda(params)
        return

    if not cuando.strip() and not encargo.strip():
        await params.result_callback(
            {
                "error": "No has dicho qué quieres cambiar.",
                "sugerencia": "Indica la hora nueva, el encargo nuevo, o los dos.",
            }
        )
        return

    momento: datetime | None = None
    if cuando.strip():
        momento = await _resolver_momento(params, cuando)
        if momento is None:
            return

    try:
        mision = await almacen.editar(id_llamada.strip(), cuando=momento, mision=encargo)
    except OSError as e:
        logger.error(f"[herramienta] editar_llamada_programada no pudo guardar: {e}")
        await params.result_callback(
            {
                "error": "No se pudo cambiar la llamada programada.",
                "sugerencia": "Dile que sigue como estaba.",
            }
        )
        return

    if mision is None:
        await params.result_callback(
            {
                "error": f"No tengo ninguna llamada pendiente con el identificador '{id_llamada}'.",
                "sugerencia": (
                    "Consulta llamadas_programadas para ver las que hay. Puede que ya "
                    "se hiciera, o que sea una tarea fija que tú no puedes cambiar."
                ),
            }
        )
        return

    logger.info(f"[herramienta] editar_llamada_programada({mision.id})")
    await params.result_callback(
        {
            "cambiada": True,
            **_resumen(mision),
            "aviso": "Confirma en voz alta el día y la hora. No leas el identificador.",
        }
    )


async def cancelar_llamada_programada(params: FunctionCallParams, id_llamada: str) -> None:
    """Retira una llamada que tenías programada, para que no se haga.

    Úsala cuando la persona te diga que ya no hace falta que la llames. Si no
    sabes el identificador, consúltalo antes con `llamadas_programadas`.

    Args:
        id_llamada: El identificador exacto que te dio `llamadas_programadas`,
            sin cambiarlo.
    """
    almacen = _almacen(params)
    if almacen is None:
        await _sin_agenda(params)
        return

    try:
        mision = await almacen.cancelar(id_llamada.strip())
    except OSError as e:
        logger.error(f"[herramienta] cancelar_llamada_programada no pudo guardar: {e}")
        await params.result_callback(
            {
                "error": "No se pudo cancelar la llamada programada.",
                "sugerencia": "Dile que sigue en pie y que avise por otro medio.",
            }
        )
        return

    if mision is None:
        await params.result_callback(
            {
                "cancelada": False,
                "error": f"No tengo ninguna llamada pendiente con el identificador '{id_llamada}'.",
                "sugerencia": (
                    "Consulta llamadas_programadas. Puede que ya se hiciera, o que "
                    "sea una tarea fija que tú no puedes cancelar."
                ),
            }
        )
        return

    logger.info(f"[herramienta] cancelar_llamada_programada({mision.id})")
    await params.result_callback({"cancelada": True, **_resumen(mision)})


async def llamadas_programadas(params: FunctionCallParams) -> None:
    """Consulta las llamadas que tienes agendadas y aún no se han hecho.

    Úsala cuando te pregunten si vas a llamar, o antes de mover o cancelar una
    llamada, para saber cuál es. Al contarlas en voz alta di el día, la hora y
    a quién, **nunca el identificador**.
    """
    almacen = _almacen(params)
    if almacen is None:
        await _sin_agenda(params)
        return

    pendientes = almacen.pendientes()
    logger.info(f"[herramienta] llamadas_programadas() -> {len(pendientes)}")
    await params.result_callback(
        {
            "total": len(pendientes),
            "llamadas": [_resumen(m) for m in pendientes],
            "aviso": "No leas los identificadores en voz alta; son para tus herramientas.",
        }
    )


#: Van en su propia lista, como las de teléfono, pero con bandera aparte: hay
#: que poder programar una llamada **dentro** de una llamada, y ahí las de
#: teléfono no están montadas. Ver `herramientas_activas`.
HERRAMIENTAS_AGENDA: list[FunctionSchema | DirectFunction] = [
    programar_llamada,
    editar_llamada_programada,
    cancelar_llamada_programada,
    llamadas_programadas,
]


__all__ = [
    "ADELANTO_MINIMO_SECS",
    "HERRAMIENTAS_AGENDA",
    "HORIZONTE_DIAS",
    "cancelar_llamada_programada",
    "editar_llamada_programada",
    "llamadas_programadas",
    "programar_llamada",
]
