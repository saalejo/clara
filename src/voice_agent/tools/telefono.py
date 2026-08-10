"""Herramientas de teléfono: contestar, colgar, marcar y buscar en la agenda.

Estas son las primeras herramientas del proyecto que **actúan sobre el mundo**.
Las tres anteriores solo leen: la peor consecuencia de que el modelo se
equivoque con `obtener_fecha_hora` es decir una hora mal. Marcar un número es
otra cosa: no se puede deshacer, y el que descuelga al otro lado es una persona.

De ahí las tres reglas de diseño que gobiernan el módulo:

**1. El pestillo `confirmado`.** Las dos herramientas que marcan llevan un
argumento obligatorio que el modelo tiene que poner en `true` a propósito. La
primera pasada no marca: devuelve a quién ha encontrado y con qué número, para
que el agente lo diga en voz alta y espere el sí. Un prompt que pide confirmar
se lo salta cualquier modelo antes o después; un argumento no.

**2. El modelo nunca desambigua.** Si hay dos "Ana", la herramienta devuelve
`estado: "ambiguo"` con la pregunta ya redactada y **no marca**. Elegir por su
cuenta entre dos personas es exactamente lo que no debe hacer.

**3. El modelo nunca ve la agenda entera.** Solo resultados de búsqueda. Es lo
que impide que "recuerde" un número de tres turnos atrás y lo marque.

Y una regla de forma, heredada del resto del proyecto: **ninguna herramienta
deja escapar una excepción**. Un fallo es un dato más que el modelo tiene que
poder contarle a la persona, no un error que rompa el turno.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources
from voice_agent.telefonia import ClienteTelefonia, ErrorTelefonia


def _cliente(params: FunctionCallParams) -> ClienteTelefonia:
    """Saca el cliente de los recursos compartidos.

    Raises:
        ErrorTelefonia: Si el agente arrancó sin telefonía.
    """
    recursos: AppResources = params.app_resources
    if recursos.telefonia is None:
        raise ErrorTelefonia(
            "este agente no tiene teléfono configurado",
            sugerencia="Dile que no puedes usar el teléfono.",
        )
    return recursos.telefonia


async def _responder_error(params: FunctionCallParams, e: ErrorTelefonia) -> None:
    """Convierte un fallo en un resultado que el modelo pueda contar."""
    logger.warning(f"[herramienta] telefonía: {e}")
    await params.result_callback(
        {
            "error": str(e),
            "sugerencia": e.sugerencia or "Dile a la persona que no se ha podido hacer.",
        }
    )


async def estado_del_telefono(params: FunctionCallParams) -> None:
    """Consulta si hay un teléfono conectado por Bluetooth y si hay alguna llamada.

    Úsala cuando te pregunten si el teléfono está conectado, cuántos contactos
    tienes, o si hay alguien al teléfono ahora mismo. Úsala también cuando otra
    herramienta de teléfono te haya fallado, para saber por qué y poder
    explicarlo.
    """
    try:
        estado = await _cliente(params).estado()
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    logger.info(f"[herramienta] estado_del_telefono() -> {estado.detalle}")
    await params.result_callback(
        {
            "descripcion": estado.detalle,
            "conectado": estado.telefono_conectado,
            "telefono": estado.telefono_nombre,
            "contactos": estado.contactos,
            "llamadas": [
                {"id": ll.id, "quien": ll.quien, "estado": ll.estado.value}
                for ll in estado.llamadas
            ],
        }
    )


async def buscar_contacto(params: FunctionCallParams, nombre: str) -> None:
    """Busca a alguien en la agenda del teléfono y devuelve su número.

    Úsala cuando te pregunten el número de alguien, o antes de llamar si no
    tienes claro a quién se refieren. Nunca te inventes un número ni uses uno
    que creas recordar: si esta herramienta no encuentra a la persona, dilo.

    Args:
        nombre: El nombre tal y como lo ha dicho la persona, aunque esté
            incompleto o suene raro. No lo corrijas ni lo completes tú: la
            búsqueda ya tolera errores de transcripción.
    """
    try:
        resultado = await _cliente(params).buscar_contactos(nombre)
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    coincidencias = resultado["coincidencias"]
    logger.info(f"[herramienta] buscar_contacto('{nombre}') -> {len(coincidencias)} resultados")

    if not coincidencias:
        await params.result_callback(
            {
                "encontrado": False,
                "mensaje": f"No hay nadie que se parezca a «{nombre}» en la agenda.",
            }
        )
        return

    await params.result_callback(
        {
            "encontrado": True,
            "ambiguo": resultado["ambiguo"],
            "pregunta": resultado["pregunta"],
            "contactos": [
                {"nombre": c.contacto.nombre, "numero": c.contacto.numero_preferido}
                for c in coincidencias
            ],
        }
    )


async def _marcar_con_pestillo(
    params: FunctionCallParams, nombre: str, numero: str, confirmado: bool
) -> None:
    """Marca, o devuelve los datos para que se confirmen antes.

    Es la mitad compartida de `llamar_a_contacto` y `llamar_a_numero`.
    """
    if not confirmado:
        logger.info(f"[herramienta] llamada a {nombre or numero} pendiente de confirmar")
        await params.result_callback(
            {
                "estado": "pendiente_de_confirmar",
                "a_quien": nombre or numero,
                "numero": numero,
                "instruccion": (
                    f"Todavía NO he llamado. Dile en voz alta que vas a llamar a "
                    f"{nombre or numero} y espera a que te confirme. Cuando te diga que sí, "
                    "vuelve a llamarme con confirmado en true."
                ),
            }
        )
        return

    try:
        llamada = await _cliente(params).marcar(numero)
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    logger.info(f"[herramienta] llamando a {nombre or numero} ({numero})")
    await params.result_callback(
        {
            "estado": "llamando",
            "a_quien": nombre or numero,
            "id_llamada": llamada.id,
            "instruccion": (
                "Ya está sonando. El audio de la llamada va por el móvil, no por aquí: "
                "dile que ya puede hablar por el teléfono."
            ),
        }
    )


async def llamar_a_contacto(params: FunctionCallParams, nombre: str, confirmado: bool) -> None:
    """Llama por teléfono a alguien de la agenda.

    Args:
        nombre: El nombre tal y como lo ha dicho la persona. No lo corrijas.
        confirmado: Ponlo en `false` la primera vez. Te diré a quién he
            encontrado y con qué número; se lo repites en voz alta y esperas su
            respuesta. Solo cuando te haya dicho que sí, vuelves a llamarme con
            `confirmado` en `true`. Si hay varias personas con ese nombre te
            devolveré la lista para que preguntes cuál: nunca elijas tú.
    """
    try:
        resultado = await _cliente(params).buscar_contactos(nombre)
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    coincidencias = resultado["coincidencias"]
    if not coincidencias:
        await params.result_callback(
            {
                "estado": "no_encontrado",
                "mensaje": f"No encuentro a «{nombre}» en la agenda. No he llamado a nadie.",
            }
        )
        return

    if resultado["ambiguo"]:
        # Aquí NO se marca, y la pregunta viene redactada para que el modelo no
        # improvise una lista de números por teléfono.
        logger.info(f"[herramienta] llamar_a_contacto('{nombre}') es ambiguo; no se marca")
        await params.result_callback(
            {
                "estado": "ambiguo",
                "pregunta": resultado["pregunta"],
                "candidatos": [c.contacto.nombre for c in coincidencias],
                "instruccion": (
                    "No he llamado a nadie. Haz esa pregunta en voz alta y, cuando te "
                    "respondan, vuelve a llamarme con el nombre completo."
                ),
            }
        )
        return

    elegido = coincidencias[0].contacto
    await _marcar_con_pestillo(params, elegido.nombre, elegido.numero_preferido, confirmado)


async def llamar_a_numero(params: FunctionCallParams, numero: str, confirmado: bool) -> None:
    """Llama a un número de teléfono que te acaban de dictar.

    Úsala SOLO si la persona te ha dicho el número en esta misma conversación.
    Para llamar a alguien de la agenda usa `llamar_a_contacto`. Nunca escribas
    aquí un número que recuerdes o que te suene: no tienes forma de saber si es
    correcto y llamarías a un desconocido.

    Args:
        numero: Los dígitos tal y como los han dictado, con el prefijo del país
            si lo han dicho.
        confirmado: `false` la primera vez; te devolveré el número para que lo
            repitas en voz alta y lo confirmen. `true` solo después de que te
            hayan dicho que sí.
    """
    limpio = "".join(c for c in numero if c.isdigit() or c == "+")
    if len(limpio.lstrip("+")) < 3:
        await params.result_callback(
            {
                "estado": "numero_invalido",
                "mensaje": f"«{numero}» no parece un número de teléfono. No he llamado.",
            }
        )
        return
    await _marcar_con_pestillo(params, "", limpio, confirmado)


async def contestar_llamada(params: FunctionCallParams) -> None:
    """Contesta la llamada que está entrando.

    Úsala cuando esté sonando una llamada y la persona te diga que la coja
    ("sí", "contesta", "cógelo"). Después de contestar, el audio de la llamada
    va por el teléfono y no por aquí: avísale de que ya puede hablar por el
    móvil.
    """
    try:
        llamada = await _cliente(params).contestar()
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    logger.info(f"[herramienta] contestada la llamada de {llamada.quien}")
    await params.result_callback(
        {
            "estado": "contestada",
            "quien": llamada.quien,
            "instruccion": "Dile que ya está contestada y que puede hablar por el móvil.",
        }
    )


async def colgar_llamada(params: FunctionCallParams) -> None:
    """Cuelga la llamada en curso, o rechaza la que está entrando.

    Úsala tanto para rechazar una llamada que suena como para terminar una
    conversación en marcha. Si hay varias, las cuelga todas.
    """
    try:
        await _cliente(params).colgar()
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    logger.info("[herramienta] llamada colgada")
    await params.result_callback({"estado": "colgada"})


async def marcar_tonos(params: FunctionCallParams, tonos: str) -> None:
    """Marca dígitos en el teclado durante una llamada en curso.

    Úsala cuando un contestador automático pida "marque 1 para...", o para
    teclear un código o una extensión.

    Args:
        tonos: Los dígitos a marcar, seguidos y sin espacios. Solo valen 0-9,
            asterisco y almohadilla.
    """
    try:
        await _cliente(params).tonos(tonos)
    except ErrorTelefonia as e:
        await _responder_error(params, e)
        return

    logger.info(f"[herramienta] tonos marcados: {tonos}")
    await params.result_callback({"estado": "marcados", "tonos": tonos})


#: Las herramientas de teléfono, **aparte** del registro principal.
#:
#: No se añaden a `HERRAMIENTAS` a propósito: así el registro de siempre —y su
#: instantánea en los tests— no cambia, y el agente sin puente de telefonía es
#: byte a byte el de antes. `bot.py` las mezcla solo cuando el sondeo confirma
#: que hay puente. Ver `herramientas_activas`.
HERRAMIENTAS_TELEFONIA: list[Any] = [
    estado_del_telefono,
    buscar_contacto,
    llamar_a_contacto,
    llamar_a_numero,
    contestar_llamada,
    colgar_llamada,
    marcar_tonos,
]


__all__ = [
    "HERRAMIENTAS_TELEFONIA",
    "buscar_contacto",
    "colgar_llamada",
    "contestar_llamada",
    "estado_del_telefono",
    "llamar_a_contacto",
    "llamar_a_numero",
    "marcar_tonos",
]
