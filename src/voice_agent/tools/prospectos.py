"""Las herramientas del perfil comercial: identidad, historial y brief.

Son el equivalente comercial de `registrar_alerta`/`finalizar_llamada` y
`historial_paciente`: con ellas la asesora reconoce a un prospecto que vuelve,
consulta qué se habló y deja el brief de requisitos estructurado para el
equipo. Escriben en el `AlmacenProspectos` compartido y siguen su doctrina:
un fallo de disco degrada, jamás tumba la conversación.

Como en todo el paquete, el docstring de cada herramienta es el esquema que ve
el modelo: instrucciones, no documentación.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from voice_agent.resources import AppResources


def _sin_almacen(recursos: AppResources) -> bool:
    return recursos.prospectos is None or not recursos.id_prospecto


async def identificar_prospecto(
    params: FunctionCallParams,
    nombre: str,
    empresa: str = "",
    contacto: str = "",
) -> None:
    """Registra quién es la persona con la que hablas, en cuanto se presente.

    Llámala apenas te diga su nombre (y su empresa, si la menciona): es lo que
    permite reconocerla si vuelve otro día. Si ya habíamos hablado con ella
    desde otro navegador, te devuelvo lo que quedó de esa conversación para
    que retomes en vez de empezar de cero — confirma con la persona antes de
    dar nada por retomado.

    Args:
        nombre: El nombre con el que se presentó.
        empresa: El negocio o empresa que mencionó, si lo dijo.
        contacto: Un teléfono o correo para contactarla, si te lo dio.
    """
    recursos: AppResources = params.app_resources
    if _sin_almacen(recursos):
        logger.info("[herramienta] identificar_prospecto sin almacén o sin id")
        await params.result_callback(
            {
                "registrado": False,
                "motivo": "Esta conversación no tiene memoria de prospectos; sigue con normalidad.",
            }
        )
        return
    almacen = recursos.prospectos
    assert almacen is not None

    # ¿Ya tiene ficha con otro id? Navegador nuevo, misma persona: se adopta
    # la ficha vieja para que la memoria siga siendo una sola.
    contexto_previo: dict[str, str] = {}
    existente = almacen.buscar_por_identidad(nombre, empresa)
    if existente is not None and existente.id != recursos.id_prospecto:
        id_conversacion = recursos.traza.id_llamada if recursos.traza else ""
        almacen.reasignar_conversacion(id_conversacion, existente.id)
        recursos.id_prospecto = existente.id
        logger.info(f"[herramienta] identificar_prospecto adopta la ficha {existente.id[:8]}…")

    almacen.identificar(recursos.id_prospecto, nombre=nombre, empresa=empresa, contacto=contacto)

    ficha = almacen.ficha(recursos.id_prospecto)
    if ficha is not None and ficha.total_conversaciones > 1:
        contexto_previo = {
            "conversaciones_anteriores": str(ficha.total_conversaciones - 1),
            "ultima_vez": ficha.ultima.momento[:10],
        }
        if ficha.ultimo_brief is not None:
            contexto_previo["necesidad_registrada"] = ficha.ultimo_brief.necesidad
            contexto_previo["proximos_pasos_registrados"] = ficha.ultimo_brief.proximos_pasos
        elif ficha.ultima.resumen:
            contexto_previo["ultimo_resumen"] = ficha.ultima.resumen

    logger.info(f"[herramienta] identificar_prospecto({nombre}, {empresa})")
    await params.result_callback({"registrado": True, **contexto_previo})


async def guardar_brief(
    params: FunctionCallParams,
    empresa_y_contacto: str,
    necesidad: str,
    caso_de_uso: str,
    proximos_pasos: str,
    canales: str = "",
    integraciones: str = "",
    plazo_y_presupuesto: str = "",
    notas: str = "",
) -> None:
    """Guarda el brief de requisitos de la conversación antes de despedirte.

    Llámala SIEMPRE al final de la conversación, cuando ya te estés
    despidiendo: es lo que le deja al equipo comercial el encargo claro. Si
    después afinas algún dato, vuelve a llamarla: la versión que cuenta es la
    última.

    Args:
        empresa_y_contacto: Quién es el prospecto: su nombre, su empresa y
            cómo contactarlo, con lo que tengas.
        necesidad: Qué problema de su negocio quiere resolver con un agente de
            voz, en frases completas.
        caso_de_uso: El agente que se le propondría: qué haría, con quién
            hablaría y en qué momento.
        proximos_pasos: Qué acordaste: quién contacta a quién y cuándo.
        canales: Por dónde atendería el agente (teléfono, web, WhatsApp…), si
            se habló.
        integraciones: Sistemas con los que tendría que hablar (agenda,
            historia clínica, CRM…), si se mencionaron.
        plazo_y_presupuesto: Plazos o presupuesto que haya mencionado.
        notas: Cualquier otro dato útil que no quepa arriba.
    """
    recursos: AppResources = params.app_resources
    id_conversacion = recursos.traza.id_llamada if recursos.traza else ""
    if _sin_almacen(recursos) or not id_conversacion:
        logger.info("[herramienta] guardar_brief sin almacén o sin conversación")
        await params.result_callback(
            {
                "guardado": False,
                "motivo": "Esta conversación no tiene memoria de prospectos; despídete con normalidad.",
            }
        )
        return
    almacen = recursos.prospectos
    assert almacen is not None

    guardado = almacen.guardar_brief(
        id_conversacion,
        recursos.id_prospecto,
        empresa_y_contacto=empresa_y_contacto.strip(),
        necesidad=necesidad.strip(),
        caso_de_uso=caso_de_uso.strip(),
        canales=canales.strip(),
        integraciones=integraciones.strip(),
        plazo_y_presupuesto=plazo_y_presupuesto.strip(),
        proximos_pasos=proximos_pasos.strip(),
        notas=notas.strip(),
    )
    if not guardado:
        await params.result_callback(
            {
                "error": "El brief no se pudo guardar en el disco.",
                "sugerencia": "Despídete con normalidad; el fallo es del sistema, no tuyo.",
            }
        )
        return

    recursos.brief_guardado = True
    logger.info(f"[herramienta] guardar_brief -> {id_conversacion}")
    await params.result_callback({"guardado": True})


async def historial_prospecto(params: FunctionCallParams) -> None:
    """Consulta las conversaciones anteriores con este prospecto.

    Úsala cuando necesites saber qué se habló las veces anteriores: qué
    necesitaba, qué se le propuso y qué quedó pendiente. Solo devuelve
    conversaciones pasadas, no la que está en curso.
    """
    recursos: AppResources = params.app_resources
    if _sin_almacen(recursos):
        logger.info("[herramienta] historial_prospecto sin almacén o sin id")
        await params.result_callback(
            {
                "historial": "ninguno",
                "motivo": "Esta conversación no tiene memoria de prospectos.",
            }
        )
        return
    almacen = recursos.prospectos
    assert almacen is not None

    conversaciones = almacen.conversaciones(recursos.id_prospecto, limite=10)
    # La conversación en curso también está registrada y todavía no cuenta
    # como "anterior": se aparta para no contársela al modelo como historial.
    id_actual = recursos.traza.id_llamada if recursos.traza else None
    anteriores = [c for c in conversaciones if c.id_conversacion != id_actual]
    logger.info(f"[herramienta] historial_prospecto -> {len(anteriores)} anteriores")
    if not anteriores:
        await params.result_callback(
            {"historial": "ninguno", "motivo": "Es la primera conversación con este prospecto."}
        )
        return

    ficha = almacen.ficha(recursos.id_prospecto)
    respuesta: dict[str, object] = {
        "nombre_conocido": ficha.prospecto.nombre if ficha else "",
        "empresa_conocida": ficha.prospecto.empresa if ficha else "",
        "total_conversaciones_anteriores": len(anteriores),
        "conversaciones": [
            {
                "fecha": c.momento,
                "resumen": c.resumen or "sin resumen; ver el brief si lo hay",
            }
            for c in anteriores
        ],
    }
    if ficha is not None and ficha.ultimo_brief is not None:
        brief = ficha.ultimo_brief
        respuesta["ultimo_brief"] = {
            "necesidad": brief.necesidad,
            "caso_de_uso": brief.caso_de_uso,
            "proximos_pasos": brief.proximos_pasos,
        }
    await params.result_callback(respuesta)


__all__ = ["guardar_brief", "historial_prospecto", "identificar_prospecto"]
