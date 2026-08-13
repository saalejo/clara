"""Vistas del panel.

Todo está cerrado por `LoginRequiredMiddleware` salvo lo que lleve
`@login_not_required` explícito, y toda acción que cambie algo es POST con CSRF.
No es exceso de celo: desde aquí se ejecutan comandos en la placa y se para el
servicio.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
    QueryDict,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from voice_agent_core import board, cobertura, corpus
from voice_agent_core.calidad import (
    CATALOGO,
    NOMBRE_CATEGORIA,
    EstadoLote,
    ResultadoEscenario,
    SolicitudCalidad,
    escenario_por_id,
    por_categoria,
)
from voice_agent_core.cobertura import Cobertura
from voice_agent_core.cron import ErrorDeCron, ExpresionCron
from voice_agent_core.estado import leer_estado
from voice_agent_core.evaluaciones import NivelAlerta
from voice_agent_core.expediente import (
    DIRECCIONES,
    TOPES,
    CriteriosExpedientes,
    leer_expediente,
    leer_traza,
    listar_expedientes,
    opciones_de_filtro,
)
from voice_agent_core.historial import HistorialPacientes
from voice_agent_core.ingesta import ProgresoIngesta, leer_progreso
from voice_agent_core.misiones import (
    CancelacionesMisiones,
    cargar_cancelaciones,
    cargar_misiones,
)
from voice_agent_core.runtime import EventoHook
from voice_agent_core.rutas import (
    dir_resultados_calidad,
    dir_resultados_tareas,
    escribir_json_atomico,
    ruta_bitacora_tareas,
    ruta_historial,
    ruta_lote_calidad,
    ruta_misiones_canceladas,
    ruta_solicitud_calidad,
)
from voice_agent_panel import agenda, control, tailer
from voice_agent_panel.context_processors import CLAVE_SESION_PERFIL, perfil_en_edicion
from voice_agent_panel.exporter import (
    ErrorDeExportacion,
    exportar,
    exportar_tareas,
    herramientas_citadas_en_el_prompt,
)
from voice_agent_panel.forms import (
    AjustesForm,
    DocumentoForm,
    HookForm,
    PerfilForm,
    PromptForm,
    ServidorMCPForm,
    TareaForm,
    TemaForm,
)
from voice_agent_panel.models import (
    AjusteAgente,
    Despliegue,
    Herramienta,
    Hook,
    LanzamientoCalidad,
    Perfil,
    Reindexado,
    RevisionCalidad,
    ServidorMCP,
    TareaProgramada,
    VersionPrompt,
)
from voice_agent_panel.sonda_mcp import ErrorDeSondeo, sondear


def _estado_servicio() -> tuple[control.EstadoUnidad | None, str]:
    """El estado del agente, o el motivo por el que no se pudo consultar."""
    try:
        return control.estado(), ""
    except control.ErrorDeControl as e:
        return None, str(e)


@dataclass(frozen=True)
class EstadoIndice:
    """Si lo que hay en el corpus está ya en el índice del agente.

    Combina tres fuentes porque ninguna basta sola: la bitácora del panel dice
    con qué corpus se lanzó la última reindexación, systemd dice cómo le fue a
    la unidad que la ejecutó —el panel encola el trabajo, no lo ejecuta— y el
    fichero de progreso, que escribe la propia ingesta, dice por dónde va y
    cuánto de lo que hay en el corpus se ha podido reaprovechar.
    """

    marca_actual: float
    ultimo: Reindexado | None
    unidad: control.EstadoUnidad | None
    progreso: ProgresoIngesta | None = None

    @property
    def sin_constancia(self) -> bool:
        """Nunca se ha reindexado desde el panel.

        Es lo normal en una instalación que se indexó por línea de órdenes, así
        que **no se avisa**: un aviso que sale siempre se aprende a ignorar.
        """
        return self.ultimo is None

    @property
    def reindexando(self) -> bool:
        """La unidad de ingesta está corriendo ahora mismo."""
        return self.unidad is not None and self.unidad.arrancando

    @property
    def fallo(self) -> bool:
        """La última reindexación terminó con error."""
        return self.unidad is not None and self.unidad.fallido

    @property
    def hay_cambios(self) -> bool:
        """El corpus se ha tocado desde la última reindexación.

        Si la unidad falló se considera viejo aunque las fechas cuadren: se pidió
        indexar y no se indexó.
        """
        if self.reindexando:
            return False
        if self.fallo:
            return True
        if self.ultimo is None:
            return False
        return self.marca_actual > self.ultimo.marca_indexada


def _estado_del_indice() -> EstadoIndice:
    """Reúne el estado del índice para la portada y la página de conocimiento.

    El `except` de aquí abajo es **el caso normal, no el excepcional**. La unidad
    de ingesta es `Type=oneshot` con `RemainAfterExit=no`, así que en cuanto
    termina bien systemd la descarga y `GetUnit` responde "Unit ... not loaded".
    Una unidad que falló, en cambio, se queda cargada. De modo que:

        no cargada  -> terminó bien (o no se ha lanzado nunca)
        activating  -> está reindexando
        failed      -> la última reindexación falló

    Y por eso `unidad = None` significa "nada que objetar", que es justo lo que
    hace falta para que un fallo de D-Bus tampoco impida ver el corpus.
    """
    return EstadoIndice(
        marca_actual=corpus.marca_de_cambio(django_settings.CORPUS_DIR),
        ultimo=Reindexado.objects.first(),
        unidad=_unidad_de_ingesta(),
        progreso=leer_progreso(django_settings.DATA_DIR),
    )


def _unidad_de_ingesta() -> control.EstadoUnidad | None:
    """El estado de la unidad de ingesta, o None si no se pudo preguntar."""
    try:
        return control.estado(django_settings.UNIDAD_INGESTA)
    except control.ErrorDeControl:
        return None


# --- Portada -----------------------------------------------------------------


def panel(request: HttpRequest) -> HttpResponse:
    """Portada: estado del servicio, de la placa y de lo que cargó el agente."""
    servicio, error_servicio = _estado_servicio()
    arranque = leer_estado(django_settings.DATA_DIR)
    perfil_activo = Perfil.activo_o_none()

    desactivadas = {
        h.nombre for h in Herramienta.objects.filter(perfil=perfil_activo, habilitada=False)
    }
    avisos = [
        f"La herramienta '{nombre}' está desactivada pero el prompt activo la sigue "
        "mencionando: el modelo dirá que la ha usado."
        for nombre in herramientas_citadas_en_el_prompt(desactivadas)
    ]
    ultimo_bueno = Despliegue.objects.exclude(resultado=Despliegue.Resultado.ERROR).first()
    perfil_enviado = (
        (ultimo_bueno.instantanea_runtime or {}).get("perfil") if ultimo_bueno else None
    )
    if perfil_activo and perfil_enviado and perfil_enviado != perfil_activo.nombre:
        avisos.append(
            f"El perfil activo es '{perfil_activo.nombre}', pero el último despliegue "
            f"envió '{perfil_enviado}'. Despliega para aplicarlo."
        )
    if arranque:
        if not arranque.tiene_llm_api_key:
            avisos.append(
                "El agente no tiene clave de LLM configurada en su .env "
                "(GEMINI_API_KEY o GROQ_API_KEY)."
            )
        avisos.extend(
            f"El servidor MCP '{s.nombre}' falló en el último arranque: {s.error}"
            for s in arranque.mcp
            if not s.conectado
        )

    indice = _estado_del_indice()
    if indice.fallo:
        avisos.append(
            "La última reindexación de la base de conocimiento falló. El agente "
            "sigue con el índice anterior."
        )
    elif indice.hay_cambios:
        avisos.append(
            "El corpus ha cambiado desde la última reindexación: el agente todavía "
            "no conoce lo que has subido o borrado."
        )

    return render(
        request,
        "panel/portada.html",
        {
            "servicio": servicio,
            "error_servicio": error_servicio,
            "arranque": arranque,
            "placa": board.estado_placa(),
            "avisos": avisos,
            "indice": indice,
            "ultimos_despliegues": Despliegue.objects.all()[:5],
            "perfil_activo": perfil_activo,
        },
    )


@login_not_required
def healthz(request: HttpRequest) -> JsonResponse:
    """Comprobación de vida, sin autenticación y sin tocar nada."""
    return JsonResponse({"ok": True})


# --- Portada pública ----------------------------------------------------------

#: Hosts en los que la raíz enseña la portada en vez de mandar al panel.
HOSTS_PORTAL = {"voz-digital.com", "www.voz-digital.com"}


@login_not_required
def portal(request: HttpRequest) -> HttpResponse:
    """La portada pública de voz-digital.com: estática, sin sesión ni datos."""
    return render(
        request,
        "panel/portal.html",
        {
            "url_voz": "https://clara.voz-digital.com",
            "url_panel": "https://panel.voz-digital.com",
        },
    )


@login_not_required
def raiz(request: HttpRequest) -> HttpResponse:
    """Reparte la raíz según el host: portada en el dominio público, panel en el resto.

    El panel no tiene enrutado por host —panel.voz-digital.com y el acceso por
    LAN llegan igual—, así que la única raíz que cambia de cara es la del
    dominio desnudo.
    """
    if request.get_host().partition(":")[0].lower() in HOSTS_PORTAL:
        return portal(request)
    return redirect("panel")


# --- Prompt y alma -----------------------------------------------------------


def prompt(request: HttpRequest) -> HttpResponse:
    """Edita el prompt del sistema creando una versión nueva."""
    perfil = perfil_en_edicion(request)
    activa = VersionPrompt.activa_de(perfil)
    if activa is None:
        # Primera vez: se siembra desde las constantes del agente, de modo que
        # lo que se enseña es exactamente lo que está corriendo.
        from voice_agent_core.prompts import MULETILLAS, PROMPT_SISTEMA, SALUDO_INICIAL

        activa = VersionPrompt.objects.create(
            perfil=perfil,
            mensaje="Valores por defecto del agente",
            prompt_sistema=PROMPT_SISTEMA,
            alma="",
            saludo_inicial=SALUDO_INICIAL,
            muletillas={k: list(v) for k, v in MULETILLAS.items()},
            activa=True,
        )

    if request.method == "POST":
        formulario = PromptForm(request.POST)
        if formulario.is_valid():
            version = formulario.save(commit=False)
            version.perfil = perfil
            version.muletillas = formulario.muletillas
            version.autor = request.user if request.user.is_authenticated else None
            version.save()
            version.activar()
            messages.success(
                request, "Versión guardada y activada. Falta desplegar para que el agente la use."
            )
            return redirect("prompt")
    else:
        formulario = PromptForm(instance=activa)

    return render(
        request,
        "panel/prompt.html",
        {
            "form": formulario,
            "activa": activa,
            "historial": VersionPrompt.objects.filter(perfil=perfil)[:30],
        },
    )


@require_POST
def volver_a_version(request: HttpRequest, pk: int) -> HttpResponse:
    """Vuelve a una versión anterior copiándola, sin reescribir el historial."""
    # Solo dentro del perfil en edición: una URL guardada de otro perfil no
    # debe activar una versión donde no toca.
    vieja = get_object_or_404(VersionPrompt, pk=pk, perfil=perfil_en_edicion(request))
    autor = request.user if request.user.is_authenticated else None
    nueva = vieja.duplicar(autor, f"Vuelta a la versión del {vieja.creado_en:%Y-%m-%d %H:%M}")
    nueva.activar()
    messages.success(request, "Se ha creado una copia de esa versión y se ha activado.")
    return redirect("prompt")


# --- Ajustes -----------------------------------------------------------------


def ajustes(request: HttpRequest) -> HttpResponse:
    """Edita los campos de `Settings` que el panel puede gobernar."""
    perfil = perfil_en_edicion(request)
    if request.method == "POST":
        formulario = AjustesForm(request.POST)
        if formulario.is_valid():
            try:
                valores = formulario.valores_a_guardar()
            except Exception as e:
                messages.error(request, str(e))
            else:
                autor = request.user if request.user.is_authenticated else None
                AjusteAgente.objects.filter(perfil=perfil).exclude(clave__in=valores).delete()
                for clave, valor in valores.items():
                    AjusteAgente.objects.update_or_create(
                        perfil=perfil,
                        clave=clave,
                        defaults={"valor": valor, "actualizado_por": autor},
                    )
                messages.success(request, "Ajustes guardados. Falta desplegar.")
                return redirect("ajustes")
    else:
        import json

        guardados = {
            a.clave: json.loads(a.valor) for a in AjusteAgente.objects.filter(perfil=perfil)
        }
        iniciales = {
            clave: ("true" if valor is True else "false" if valor is False else valor)
            for clave, valor in guardados.items()
        }
        formulario = AjustesForm(initial=iniciales)

    return render(request, "panel/ajustes.html", {"form": formulario})


# --- Herramientas ------------------------------------------------------------


def herramientas(request: HttpRequest) -> HttpResponse:
    """Enciende y apaga las herramientas que ve el modelo."""
    perfil = perfil_en_edicion(request)
    arranque = leer_estado(django_settings.DATA_DIR)
    conocidas = arranque.herramientas if arranque else []

    if request.method == "POST":
        activas = set(request.POST.getlist("activa"))
        # Se guarda una fila por cada herramienta conocida, para que el estado
        # sea explícito y no dependa de que el agente vuelva a publicarlas.
        for expuesta in conocidas:
            Herramienta.objects.update_or_create(
                perfil=perfil,
                nombre=expuesta.nombre,
                defaults={"habilitada": expuesta.nombre in activas},
            )
        messages.success(request, "Herramientas guardadas. Falta desplegar.")
        return redirect("herramientas")

    estados = {h.nombre: h.habilitada for h in Herramienta.objects.filter(perfil=perfil)}
    filas = [
        {
            "nombre": e.nombre,
            "origen": e.origen,
            "descripcion": e.descripcion,
            "esquema": e.esquema,
            "habilitada": estados.get(e.nombre, True),
        }
        for e in conocidas
    ]
    return render(request, "panel/herramientas.html", {"filas": filas, "arranque": arranque})


# --- Servidores MCP ----------------------------------------------------------


def mcp(request: HttpRequest) -> HttpResponse:
    """Lista los servidores MCP y elige cuáles usa el perfil en edición."""
    perfil = perfil_en_edicion(request)

    if request.method == "POST":
        elegidos = request.POST.getlist("habilitado")
        perfil.mcp_habilitados.set(ServidorMCP.objects.filter(pk__in=elegidos))
        messages.success(request, "Selección guardada. Falta desplegar.")
        return redirect("mcp")

    arranque = leer_estado(django_settings.DATA_DIR)
    por_nombre = {s.nombre: s for s in (arranque.mcp if arranque else [])}
    habilitados = set(perfil.mcp_habilitados.values_list("pk", flat=True))
    return render(
        request,
        "panel/mcp.html",
        {
            "servidores": [
                {"obj": s, "arranque": por_nombre.get(s.nombre), "habilitado": s.pk in habilitados}
                for s in ServidorMCP.objects.all()
            ]
        },
    )


def mcp_editar(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Alta o edición de un servidor MCP."""
    instancia = get_object_or_404(ServidorMCP, pk=pk) if pk else ServidorMCP()
    if request.method == "POST":
        formulario = ServidorMCPForm(request.POST, instance=instancia)
        if formulario.is_valid():
            formulario.save()
            messages.success(request, "Servidor guardado. Falta desplegar.")
            return redirect("mcp")
    else:
        formulario = ServidorMCPForm(instance=instancia)
    return render(request, "panel/mcp_editar.html", {"form": formulario, "servidor": instancia})


@require_POST
def mcp_borrar(request: HttpRequest, pk: int) -> HttpResponse:
    """Borra un servidor MCP."""
    get_object_or_404(ServidorMCP, pk=pk).delete()
    messages.success(request, "Servidor borrado. Falta desplegar.")
    return redirect("mcp")


@require_POST
def mcp_sondear(request: HttpRequest, pk: int) -> HttpResponse:
    """Pregunta al servidor qué herramientas ofrece, antes de desplegar."""
    servidor = get_object_or_404(ServidorMCP, pk=pk)
    try:
        nombres = sondear(servidor)
    except ErrorDeSondeo as e:
        servidor.ultimo_error = str(e)
        servidor.herramientas_descubiertas = []
        messages.error(request, f"No se pudo sondear: {e}")
    else:
        servidor.ultimo_error = ""
        servidor.herramientas_descubiertas = nombres
        messages.success(request, f"Ofrece {len(nombres)} herramienta(s): {', '.join(nombres)}")
    from django.utils import timezone

    servidor.ultimo_sondeo = timezone.now()
    servidor.save(update_fields=["ultimo_sondeo", "ultimo_error", "herramientas_descubiertas"])
    return redirect("mcp")


# --- Base de conocimiento ----------------------------------------------------
#
# Es la única parte del panel que escribe ficheros en una ruta derivada de la
# petición. Dos reglas de las que no se sale:
#
#   1. El tema y el documento viajan en el CUERPO del POST, nunca en la URL. Así
#      no hay ningún convertidor de ruta que alguien pueda "arreglar" algún día
#      para que acepte barras.
#   2. Toda ruta se compone con `corpus.resolver`, que valida cada componente y
#      además comprueba que el resultado no se sale del corpus.
#
# Y una diferencia con el resto del panel que conviene tener presente: esto NO
# pasa por `exporter.py`. Los documentos son ficheros de verdad en el volumen
# compartido, no configuración exportada, así que el ciclo no es
# "guardar -> desplegar" sino "subir -> reindexar".


def conocimiento(request: HttpRequest) -> HttpResponse:
    """Lista los temas y sus documentos, y sube documentos nuevos."""
    corpus_dir = django_settings.CORPUS_DIR

    if request.method == "POST":
        formulario = DocumentoForm(request.POST, request.FILES)
        if formulario.is_valid():
            archivo = formulario.cleaned_data["archivo"]
            tema = formulario.cleaned_data["tema"]
            try:
                # `chunks()` y no `read()`: el contenedor del panel tiene 256 MB
                # y así el tope se comprueba mientras se escribe.
                documento = corpus.guardar_documento(
                    corpus_dir, tema, archivo.name, archivo.chunks()
                )
            except (corpus.NombreInvalido, corpus.ErrorDeCorpus) as e:
                messages.error(request, str(e))
            else:
                messages.success(
                    request,
                    f"Guardado como '{documento.nombre}'. Falta reindexar para que "
                    "el agente lo aprenda.",
                )
                return redirect("conocimiento")
    else:
        formulario = DocumentoForm()

    # Los alias viven en `data/`, no en el corpus, y por eso se cosen aquí en
    # vez de venir en el `Tema` (ver `rutas.ruta_alias_temas`).
    alias = cobertura.cargar_alias(django_settings.DATA_DIR)
    return render(
        request,
        "panel/conocimiento.html",
        {
            "temas": [
                {
                    "nombre": tema.nombre,
                    "es_raiz": tema.es_raiz,
                    "documentos": tema.documentos,
                    "alias": ", ".join(alias.get(tema.nombre, ())),
                }
                for tema in corpus.inventario(corpus_dir)
            ],
            "form_documento": formulario,
            "form_tema": TemaForm(),
            "indice": _estado_del_indice(),
            "corpus_dir": corpus_dir,
            "ignorados": corpus.documentos_ignorados(corpus_dir),
            "extensiones": ", ".join(sorted(corpus.EXTENSIONES_SOPORTADAS)),
        },
    )


def conocimiento_progreso(request: HttpRequest) -> JsonResponse:
    """El avance de la reindexación, para la barra de la página de Conocimiento.

    Se sirve como una consulta corta que el navegador repite cada segundo, y no
    como un flujo SSE como el de los logs. La diferencia no es de gusto: el log
    es un chorro sin final que hay que *transmitir*, mientras que esto es un
    retrato pequeño que se relee. Y una petición por segundo no deja ningún hilo
    del panel ocupado mientras dura la reindexación —que con el corpus clínico
    puede irse a la media hora—, mientras que una pestaña olvidada abierta sobre
    un SSE sí lo dejaría.

    Combina el fichero que escribe la ingesta con el estado de la unidad: el
    fichero dice por dónde va, y systemd dice si sigue viva. Hace falta lo
    segundo porque una ingesta que muera de golpe —OOM, por ejemplo— deja el
    fichero congelado a mitad y sin nadie que lo desmienta.
    """
    unidad = _unidad_de_ingesta()
    progreso = leer_progreso(django_settings.DATA_DIR)
    datos: dict[str, Any] = {
        "en_marcha": unidad is not None and unidad.arrancando,
        "fallo": unidad is not None and unidad.fallido,
    }
    if progreso is not None:
        datos |= {
            "fase": str(progreso.fase),
            "porcentaje": progreso.porcentaje,
            "terminada": progreso.terminada,
            "tema": progreso.tema_actual,
            "documento": progreso.documento_actual,
            "documentos_total": progreso.documentos_total,
            "documentos_sin_cambios": progreso.documentos_sin_cambios,
            "documentos_pendientes": progreso.documentos_pendientes,
            "documentos_hechos": progreso.documentos_hechos,
            "fragmentos_total": progreso.fragmentos_total,
            "fragmentos_nuevos": progreso.fragmentos_nuevos,
            "fragmentos_olvidados": progreso.fragmentos_olvidados,
            "duracion_s": round(progreso.duracion_s, 1),
            "error": progreso.error,
        }
    return JsonResponse(datos)


@require_POST
def tema_crear(request: HttpRequest) -> HttpResponse:
    """Crea la carpeta de un tema."""
    formulario = TemaForm(request.POST)
    if not formulario.is_valid():
        messages.error(request, "; ".join(formulario.errors.get("nombre", ["Nombre no válido."])))
        return redirect("conocimiento")

    try:
        nombre = corpus.crear_tema(django_settings.CORPUS_DIR, formulario.cleaned_data["nombre"])
    except (corpus.NombreInvalido, corpus.ErrorDeCorpus) as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Tema '{nombre}' creado. Sube dentro sus documentos.")
    return redirect("conocimiento")


@require_POST
def tema_alias(request: HttpRequest) -> HttpResponse:
    """Declara con qué nombres reconoce el agente la cirugía de un tema.

    No toca el corpus ni el índice: los alias solo alimentan la puerta de
    cobertura, y el agente los relee en cada consulta. Por eso el mensaje dice
    que ya está aplicado y no que falte reindexar, a diferencia de subir o
    borrar un documento.
    """
    tema = request.POST.get("tema", "")
    if tema not in corpus.listar_temas(django_settings.CORPUS_DIR):
        messages.error(request, f"El tema '{tema}' no existe.")
        return redirect("conocimiento")

    alias = cobertura.cargar_alias(django_settings.DATA_DIR)
    nombres = [texto.strip() for texto in request.POST.get("alias", "").split(",")]
    alias[tema] = tuple(n for n in nombres if n)
    try:
        cobertura.guardar_alias(django_settings.DATA_DIR, alias)
    except OSError as e:
        messages.error(request, f"No se pudieron guardar los nombres: {e}")
    else:
        cuantos = len(alias[tema])
        messages.success(
            request,
            f"'{tema}': {cuantos} nombre(s) guardado(s). El agente ya los usa; "
            "no hace falta reindexar ni reiniciar."
            if cuantos
            else f"'{tema}' vuelve a reconocerse solo por su nombre.",
        )
    return redirect("conocimiento")


@require_POST
def tema_borrar(request: HttpRequest) -> HttpResponse:
    """Borra la carpeta de un tema, que tiene que estar vacía."""
    tema = request.POST.get("tema", "")
    try:
        corpus.borrar_tema(django_settings.CORPUS_DIR, tema)
    except (corpus.NombreInvalido, corpus.ErrorDeCorpus) as e:
        messages.error(request, str(e))
    else:
        messages.success(
            request,
            f"Tema '{tema}' borrado. Falta reindexar para que su colección desaparezca del índice.",
        )
    return redirect("conocimiento")


@require_POST
def documento_borrar(request: HttpRequest) -> HttpResponse:
    """Borra un documento del corpus."""
    tema = request.POST.get("tema", "")
    nombre = request.POST.get("nombre", "")
    try:
        corpus.borrar_documento(django_settings.CORPUS_DIR, tema, nombre)
    except (corpus.NombreInvalido, corpus.ErrorDeCorpus) as e:
        messages.error(request, str(e))
    else:
        messages.success(
            request,
            f"Documento '{nombre}' borrado. Falta reindexar para que el agente lo olvide.",
        )
    return redirect("conocimiento")


def _lanzar_reindexado(request: HttpRequest) -> None:
    """Pide a systemd que reindexe y deja constancia de con qué corpus.

    La marca se toma **antes** de lanzar: si se tomara después, un documento
    subido mientras la ingesta corría quedaría dado por indexado sin estarlo.
    """
    autor = request.user if request.user.is_authenticated else None
    marca = corpus.marca_de_cambio(django_settings.CORPUS_DIR)

    servicio_actual, _ = _estado_servicio()
    if servicio_actual and servicio_actual.activo:
        messages.warning(
            request,
            "Ojo: reindexar mientras el agente conversa es la combinación que más "
            "cerca está de quedarse sin memoria en esta placa.",
        )

    try:
        control.lanzar_ingesta()
    except control.ErrorDeControl as e:
        Reindexado.objects.create(
            autor=autor,
            resultado=Reindexado.Resultado.ERROR,
            detalle=str(e),
            marca_indexada=marca,
        )
        messages.error(request, str(e))
        return

    Reindexado.objects.create(
        autor=autor, resultado=Reindexado.Resultado.LANZADO, marca_indexada=marca
    )
    messages.success(
        request,
        "Reindexación lanzada. El agente la recoge en caliente, sin reiniciarse.",
    )


# --- Hooks -------------------------------------------------------------------


def hooks(request: HttpRequest) -> HttpResponse:
    """Lista los hooks configurados y elige cuáles activa el perfil en edición."""
    perfil = perfil_en_edicion(request)

    if request.method == "POST":
        elegidos = request.POST.getlist("habilitado")
        perfil.hooks_habilitados.set(Hook.objects.filter(pk__in=elegidos))
        messages.success(request, "Selección guardada. Falta desplegar.")
        return redirect("hooks")

    habilitados = set(perfil.hooks_habilitados.values_list("pk", flat=True))
    return render(
        request,
        "panel/hooks.html",
        {
            "hooks": [{"obj": h, "habilitado": h.pk in habilitados} for h in Hook.objects.all()],
            "eventos": [e.value for e in EventoHook],
            "comandos_permitidos": django_settings.HOOKS_COMANDO_PERMITIDOS,
        },
    )


def hook_editar(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Alta o edición de un hook."""
    instancia = get_object_or_404(Hook, pk=pk) if pk else Hook()
    if request.method == "POST":
        formulario = HookForm(request.POST, instance=instancia)
        if formulario.is_valid():
            formulario.save()
            messages.success(request, "Hook guardado. Falta desplegar.")
            return redirect("hooks")
    else:
        formulario = HookForm(instance=instancia)
    return render(request, "panel/hook_editar.html", {"form": formulario, "hook": instancia})


@require_POST
def hook_borrar(request: HttpRequest, pk: int) -> HttpResponse:
    """Borra un hook."""
    get_object_or_404(Hook, pk=pk).delete()
    messages.success(request, "Hook borrado. Falta desplegar.")
    return redirect("hooks")


# --- Tareas programadas ------------------------------------------------------


def _exportar_tareas_avisando(request: HttpRequest) -> None:
    """Exporta `tareas.json` y traduce un fallo de disco a un mensaje.

    Las tareas no pasan por el botón de desplegar: cada cambio se exporta al
    guardarlo y el agente lo recoge en caliente. Si el disco falla, lo guardado
    sigue en la base de datos y volverá a intentarse en el siguiente cambio.
    """
    try:
        exportar_tareas(django_settings.DATA_DIR)
    except OSError as e:
        messages.error(
            request,
            f"La tarea quedó guardada pero no se pudo exportar al agente: {e}",
        )
        return
    messages.success(request, "Guardado. El agente lo recoge en caliente, sin reiniciar.")


def tareas(request: HttpRequest) -> HttpResponse:
    """Lista las tareas programadas y conmuta cuáles están habilitadas."""
    if request.method == "POST":
        elegidas = set(request.POST.getlist("habilitada"))
        for tarea in TareaProgramada.objects.all():
            tarea.habilitada = str(tarea.pk) in elegidas
            tarea.save(update_fields=["habilitada"])
        _exportar_tareas_avisando(request)
        return redirect("tareas")

    ahora = datetime.now()
    filas = []
    for tarea in TareaProgramada.objects.all():
        try:
            proxima = ExpresionCron.parse(tarea.cron).siguiente(ahora)
        except ErrorDeCron:
            proxima = None  # guardada con una versión anterior del parser
        filas.append({"obj": tarea, "proxima": proxima})
    return render(
        request,
        "panel/tareas.html",
        {"tareas": filas, "misiones": cargar_misiones(django_settings.DATA_DIR).pendientes},
    )


@require_POST
def mision_cancelar(request: HttpRequest) -> HttpResponse:
    """Pide al agente que retire una misión puntual suya.

    Aquí no se borra nada. El dueño de `misiones_agente.json` es el agente
    —doctrina de un fichero, un escritor: si el panel escribiera ahí, pisaría
    lo que el agente acabara de apuntar en una conversación—, así que lo que se
    escribe es la petición, en el fichero que el planificador consulta por
    mtime en su siguiente vuelta.

    El id va en el CUERPO del POST y no en la ruta, como en el resto del panel:
    no es un entero de la base de datos, es un identificador que se inventó el
    agente y que puede acabar siendo carpeta.

    De paso se podan los ids que ya no correspondan a ninguna misión pendiente,
    para que el fichero no crezca sin fin. Entre leer las misiones y escribir
    esto el agente puede haber cambiado el suyo, pero da igual: **los ids no se
    reutilizan jamás** —llevan marca de tiempo y cuatro caracteres al azar—, así
    que una cancelación rezagada no puede acertarle a otra misión. No hace falta
    ningún candado, y además no podría haberlo entre dos contenedores.
    """
    id_mision = request.POST.get("id_mision", "").strip()
    vivas = {m.id for m in cargar_misiones(django_settings.DATA_DIR).pendientes}
    previos = set(cargar_cancelaciones(django_settings.DATA_DIR).ids)
    ids = sorted((previos & vivas) | ({id_mision} & vivas))
    try:
        escribir_json_atomico(
            ruta_misiones_canceladas(django_settings.DATA_DIR),
            CancelacionesMisiones(generado_en=datetime.now(), ids=ids).model_dump(mode="json"),
        )
    except OSError as e:
        messages.error(request, f"No se pudo pedir la cancelación: {e}")
        return redirect("tareas")

    if id_mision in vivas:
        messages.success(
            request, "Pedida la cancelación. El agente la recoge en menos de un minuto."
        )
    else:
        messages.warning(request, "Esa llamada ya no estaba pendiente; no hay nada que cancelar.")
    return redirect("tareas")


def tarea_editar(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Alta o edición de una tarea, con buscador de contactos de la agenda.

    El buscador es una sub-acción del mismo formulario (`accion=buscar_contacto`):
    re-renderiza con los candidatos sin guardar nada. Elegir uno rellena los
    campos de contacto por parte del navegador; el número queda congelado en la
    tarea al guardar.
    """
    instancia = get_object_or_404(TareaProgramada, pk=pk) if pk else TareaProgramada()
    candidatos: list[dict[str, Any]] = []

    if request.method == "POST":
        formulario = TareaForm(request.POST, instance=instancia)
        if request.POST.get("accion") == "buscar_contacto":
            consulta = request.POST.get("contacto_nombre", "").strip()
            candidatos = agenda.buscar_contactos(consulta)
            if not candidatos:
                messages.warning(
                    request,
                    "Sin resultados. ¿Está el puente de telefonía en marcha y el móvil "
                    "emparejado? También puedes teclear el número a mano.",
                )
            formulario.is_valid()  # puebla cleaned_data para re-renderizar sin perder lo escrito
        elif formulario.is_valid():
            formulario.save()
            _exportar_tareas_avisando(request)
            return redirect("tareas")
    else:
        formulario = TareaForm(instance=instancia)

    return render(
        request,
        "panel/tarea_editar.html",
        {"form": formulario, "tarea": instancia, "candidatos": candidatos},
    )


@require_POST
def tarea_borrar(request: HttpRequest, pk: int) -> HttpResponse:
    """Borra una tarea. Sus resultados guardados se quedan en disco."""
    get_object_or_404(TareaProgramada, pk=pk).delete()
    _exportar_tareas_avisando(request)
    return redirect("tareas")


def _siguiente_tope(actual: int) -> int | None:
    """El siguiente peldaño de la escalera de tamaños, o `None` si ya está arriba."""
    return next((tope for tope in TOPES if tope > actual), None)


def _consulta_sin(parametros: QueryDict, *quitar: str) -> str:
    """La cadena de consulta actual sin los parámetros dados.

    Es lo que hace que «Ver más» y el enlace al detalle conserven los filtros:
    un filtro que no sobrevive a un clic no sirve de nada.
    """
    copia = parametros.copy()
    for clave in quitar:
        copia.pop(clave, None)
    return copia.urlencode()


def evaluaciones(request: HttpRequest) -> HttpResponse:
    """Las evaluaciones clínicas: una llamada por fila, filtrable y navegable.

    Cada fila es la fusión de lo que el agente dejó en dos sitios —los JSON de
    `registrar_alerta`/`finalizar_llamada` y la fila del historial SQLite—, que
    es lo que hace `voice_agent_core.expediente`. El panel solo lee.

    Es la única página del panel con formulario GET, y a propósito: un filtro
    tiene que sobrevivir a recargar y a compartir el enlace.
    """
    criterios, avisos = CriteriosExpedientes.desde_parametros(request.GET)
    resultado = listar_expedientes(django_settings.DATA_DIR, criterios)
    opciones = opciones_de_filtro(
        django_settings.DATA_DIR, (e.procedimiento for e in resultado.expedientes)
    )
    return render(
        request,
        "panel/evaluaciones.html",
        {
            "criterios": criterios,
            "avisos": avisos,
            "resultado": resultado,
            "opciones": opciones,
            "niveles": list(NivelAlerta),
            "coberturas": list(Cobertura),
            "direcciones": DIRECCIONES,
            "siguiente_tope": _siguiente_tope(criterios.tope),
            "consulta": _consulta_sin(request.GET, "limite"),
        },
    )


def evaluacion_detalle(request: HttpRequest, id_llamada: str) -> HttpResponse:
    """Todo lo de una llamada en un sitio, incluida su traza documental.

    La ficha del paciente solo se pide si la llamada llegó a abrirla: las de
    navegador, número oculto o aplicación no tienen, y eso es diseño y no
    avería (ver `numero_identificable`).
    """
    expediente = leer_expediente(django_settings.DATA_DIR, id_llamada)
    if expediente is None:
        raise Http404("Esa llamada no consta ni en los ficheros ni en el historial.")
    ficha = None
    if expediente.numero:
        historial = HistorialPacientes(ruta_historial(django_settings.DATA_DIR))
        ficha = historial.ficha(expediente.numero)
    return render(
        request,
        "panel/evaluacion_detalle.html",
        {
            "e": expediente,
            "ficha": ficha,
            "traza": leer_traza(django_settings.DATA_DIR, id_llamada),
            "consulta": request.GET.urlencode(),
        },
    )


def pacientes(request: HttpRequest) -> HttpResponse:
    """El padrón de pacientes: qué números han llamado y cuándo fue la última vez.

    Sale de la base SQLite que el agente escribe en el volumen compartido
    (`data/evaluaciones/historial.sqlite3`); el panel solo lee. El expediente de
    cada llamada no se repite aquí: vive en Evaluaciones, a un clic desde cada
    ficha. Si el fichero no existe todavía, la página sale vacía.
    """
    historial = HistorialPacientes(ruta_historial(django_settings.DATA_DIR))
    return render(request, "panel/pacientes.html", {"fichas": historial.pacientes()})


# --- Calidad -----------------------------------------------------------------


def _resultados_calidad() -> list[ResultadoEscenario]:
    """Lee todos los expedientes de calidad, tolerando ficheros a medias.

    El runner escribe uno por ejecución en `data/calidad/resultados/`; el panel
    solo lee, igual que con las evaluaciones y las tareas.
    """
    carpeta = dir_resultados_calidad(django_settings.DATA_DIR)
    resultados: list[ResultadoEscenario] = []
    if carpeta.is_dir():
        for fichero in carpeta.glob("*.json"):
            try:
                resultados.append(
                    ResultadoEscenario.model_validate_json(fichero.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                continue
    return resultados


def _leer_ejecucion(id_ejecucion: str) -> ResultadoEscenario | None:
    """Lee un expediente concreto por su id, o None si no existe o no valida."""
    ruta = dir_resultados_calidad(django_settings.DATA_DIR) / f"{id_ejecucion}.json"
    if not ruta.is_file():
        return None
    try:
        return ResultadoEscenario.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _veredicto_efectivo(
    resultado: ResultadoEscenario | None, revision: RevisionCalidad | None
) -> str:
    """El veredicto que manda: la revisión manual pesa más que el juez.

    Returns:
        "aprobado", "fallo", "error" o "sin" (sin ejecutar todavía).
    """
    if revision is not None:
        return revision.veredicto
    if resultado is None:
        return "sin"
    if resultado.estado == "error":
        return "error"
    if resultado.veredicto is not None:
        return "aprobado" if resultado.veredicto.aprobado else "fallo"
    return "sin"


def _lote_en_curso() -> tuple[bool, EstadoLote | None]:
    """Si hay un lote corriendo y su progreso, para el aviso de la matriz."""
    try:
        en_marcha = control.estado_calidad().activo
    except control.ErrorDeControl:
        en_marcha = False
    lote = None
    ruta = ruta_lote_calidad(django_settings.DATA_DIR)
    if ruta.is_file():
        try:
            lote = EstadoLote.model_validate_json(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lote = None
    return en_marcha, lote


def calidad(request: HttpRequest) -> HttpResponse:
    """La matriz de escenarios adversarios, agrupada por categoría.

    Cada escenario muestra el veredicto de su última ejecución (con la revisión
    manual superpuesta si la hay) y un botón para volver a ensayarlo. Los
    resultados son ficheros del runner; la matriz es el catálogo del código.
    """
    ultimos: dict[str, ResultadoEscenario] = {}
    for resultado in _resultados_calidad():
        actual = ultimos.get(resultado.escenario_id)
        if actual is None or resultado.momento > actual.momento:
            ultimos[resultado.escenario_id] = resultado

    revisiones = {
        r.id_ejecucion: r
        for r in RevisionCalidad.objects.filter(
            id_ejecucion__in=[r.id_ejecucion for r in ultimos.values()]
        )
    }

    grupos = []
    for categoria, escenarios in por_categoria().items():
        tarjetas = []
        for escenario in escenarios:
            ultimo = ultimos.get(escenario.id)
            revision = revisiones.get(ultimo.id_ejecucion) if ultimo else None
            tarjetas.append(
                {
                    "escenario": escenario,
                    "resultado": ultimo,
                    "revision": revision,
                    "veredicto": _veredicto_efectivo(ultimo, revision),
                }
            )
        grupos.append({"nombre": NOMBRE_CATEGORIA[categoria], "tarjetas": tarjetas})

    en_marcha, lote = _lote_en_curso()
    return render(
        request,
        "panel/calidad.html",
        {"grupos": grupos, "en_marcha": en_marcha, "lote": lote},
    )


@require_POST
def calidad_lanzar(request: HttpRequest) -> HttpResponse:
    """Encola un lote de escenarios y arranca el runner por systemd.

    Como el reindexado, escribe la solicitud en disco **antes** de arrancar la
    unidad y deja una fila de bitácora; el panel encola, no afirma que terminó.
    """
    if request.POST.get("todos") == "1":
        ids = [e.id for e in CATALOGO]
    else:
        escenario_id = request.POST.get("escenario", "")
        if escenario_por_id(escenario_id) is None:
            raise Http404("Escenario desconocido.")
        ids = [escenario_id]

    en_marcha, _ = _lote_en_curso()
    if en_marcha:
        messages.warning(request, "Ya hay un lote de calidad en marcha; espera a que termine.")
        return redirect("calidad")

    autor = request.user if request.user.is_authenticated else None
    id_lote = f"panel-{datetime.now():%Y%m%d-%H%M%S}"
    solicitud = SolicitudCalidad(
        id_lote=id_lote,
        momento=datetime.now().isoformat(timespec="seconds"),
        escenarios=ids,
        autor=getattr(autor, "username", ""),
    )
    try:
        escribir_json_atomico(
            ruta_solicitud_calidad(django_settings.DATA_DIR), solicitud.model_dump(mode="json")
        )
    except OSError as e:
        messages.error(request, f"No se pudo dejar la solicitud en disco: {e}")
        return redirect("calidad")

    try:
        control.lanzar_calidad()
    except control.ErrorDeControl as e:
        LanzamientoCalidad.objects.create(
            autor=autor,
            resultado=LanzamientoCalidad.Resultado.ERROR,
            detalle=str(e),
            escenarios=ids,
        )
        messages.error(request, str(e))
        return redirect("calidad")

    LanzamientoCalidad.objects.create(
        autor=autor, resultado=LanzamientoCalidad.Resultado.LANZADO, escenarios=ids
    )
    messages.success(
        request,
        f"Lanzados {len(ids)} escenario(s). Recarga la página para ver el progreso y los veredictos.",
    )
    return redirect("calidad")


def calidad_escenario(request: HttpRequest, escenario_id: str) -> HttpResponse:
    """El historial de ejecuciones de un escenario concreto."""
    escenario = escenario_por_id(escenario_id)
    if escenario is None:
        raise Http404("Escenario desconocido.")

    ejecuciones = sorted(
        (r for r in _resultados_calidad() if r.escenario_id == escenario_id),
        key=lambda r: r.momento,
        reverse=True,
    )
    revisiones = {
        r.id_ejecucion: r
        for r in RevisionCalidad.objects.filter(
            id_ejecucion__in=[e.id_ejecucion for e in ejecuciones]
        )
    }
    filas = [
        {
            "resultado": e,
            "revision": revisiones.get(e.id_ejecucion),
            "veredicto": _veredicto_efectivo(e, revisiones.get(e.id_ejecucion)),
        }
        for e in ejecuciones
    ]
    return render(request, "panel/calidad_escenario.html", {"escenario": escenario, "filas": filas})


def calidad_ejecucion(request: HttpRequest, id_ejecucion: str) -> HttpResponse:
    """El detalle de una ejecución: transcripción, veredicto y revisión manual."""
    resultado = _leer_ejecucion(id_ejecucion)
    if resultado is None:
        raise Http404("Ejecución desconocida.")

    if request.method == "POST":
        accion = request.POST.get("accion", "")
        if accion == "quitar_revision":
            RevisionCalidad.objects.filter(id_ejecucion=id_ejecucion).delete()
            messages.success(request, "Revisión retirada; vuelve a mandar el veredicto del juez.")
        elif accion == "revisar":
            veredicto = request.POST.get("veredicto", "")
            if veredicto not in dict(RevisionCalidad.Veredicto.choices):
                messages.error(request, "Veredicto no válido.")
            else:
                autor = request.user if request.user.is_authenticated else None
                RevisionCalidad.objects.update_or_create(
                    id_ejecucion=id_ejecucion,
                    defaults={
                        "veredicto": veredicto,
                        "nota": request.POST.get("nota", "").strip(),
                        "autor": autor,
                    },
                )
                messages.success(request, "Revisión guardada.")
        return redirect("calidad_ejecucion", id_ejecucion=id_ejecucion)

    escenario = escenario_por_id(resultado.escenario_id)
    revision = RevisionCalidad.objects.filter(id_ejecucion=id_ejecucion).first()
    return render(
        request,
        "panel/calidad_ejecucion.html",
        {
            "resultado": resultado,
            "escenario": escenario,
            "revision": revision,
            "veredicto": _veredicto_efectivo(resultado, revision),
        },
    )


def tarea_resultados(request: HttpRequest, pk: int) -> HttpResponse:
    """Las respuestas guardadas y la bitácora de ejecuciones de una tarea.

    Todo sale de ficheros que escribe el agente en el volumen compartido, igual
    que `estado_arranque.json`: el panel solo lee.
    """
    tarea = get_object_or_404(TareaProgramada, pk=pk)

    resultados = []
    carpeta = dir_resultados_tareas(django_settings.DATA_DIR) / tarea.nombre
    if carpeta.is_dir():
        for fichero in sorted(carpeta.iterdir(), reverse=True)[:50]:
            try:
                resultados.append(json.loads(fichero.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # un fichero a medias no arruina la página

    ejecuciones = []
    bitacora = ruta_bitacora_tareas(django_settings.DATA_DIR)
    if bitacora.is_file():
        try:
            lineas = bitacora.read_text(encoding="utf-8").splitlines()
        except OSError:
            lineas = []
        for linea in reversed(lineas):
            try:
                entrada = json.loads(linea)
            except ValueError:
                continue
            if entrada.get("id_tarea") == tarea.nombre:
                ejecuciones.append(entrada)
            if len(ejecuciones) >= 50:
                break

    return render(
        request,
        "panel/tarea_resultados.html",
        {"tarea": tarea, "resultados": resultados, "ejecuciones": ejecuciones},
    )


# --- Perfiles ----------------------------------------------------------------


def perfiles_lista(request: HttpRequest) -> HttpResponse:
    """Lista los perfiles y da de alta nuevos."""
    if request.method == "POST":
        formulario = PerfilForm(request.POST)
        if formulario.is_valid():
            perfil = formulario.save()
            messages.success(
                request,
                f"Perfil '{perfil.nombre}' creado. Actívalo cuando esté listo: "
                "hasta entonces no se despliega.",
            )
            return redirect("perfiles")
    else:
        formulario = PerfilForm()

    return render(
        request,
        "panel/perfiles.html",
        {"perfiles": Perfil.objects.all(), "form": formulario},
    )


def perfil_editar(request: HttpRequest, pk: int) -> HttpResponse:
    """Edita el nombre y la descripción de un perfil."""
    perfil = get_object_or_404(Perfil, pk=pk)
    if request.method == "POST":
        formulario = PerfilForm(request.POST, instance=perfil)
        if formulario.is_valid():
            formulario.save()
            messages.success(request, "Perfil guardado.")
            return redirect("perfiles")
    else:
        formulario = PerfilForm(instance=perfil)
    return render(request, "panel/perfil_editar.html", {"form": formulario, "perfil": perfil})


@require_POST
def perfil_activar(request: HttpRequest, pk: int) -> HttpResponse:
    """Marca un perfil como el activo. No despliega."""
    perfil = get_object_or_404(Perfil, pk=pk)
    perfil.activar()
    messages.success(
        request,
        f"Perfil '{perfil.nombre}' activado. Falta desplegar para que el agente lo use.",
    )
    return redirect("perfiles")


@require_POST
def perfil_seleccionar(request: HttpRequest, pk: int) -> HttpResponse:
    """Elige qué perfil editan las páginas de prompt, ajustes y demás."""
    perfil = get_object_or_404(Perfil, pk=pk)
    request.session[CLAVE_SESION_PERFIL] = perfil.pk
    messages.success(request, f"Editando el perfil '{perfil.nombre}'.")
    return redirect("perfiles")


@require_POST
def perfil_duplicar(request: HttpRequest, pk: int) -> HttpResponse:
    """Crea una copia completa de un perfil, sin activarla."""
    from django.db import IntegrityError

    perfil = get_object_or_404(Perfil, pk=pk)
    nombre = request.POST.get("nombre", "").strip() or f"{perfil.nombre} (copia)"
    try:
        copia = perfil.duplicar(nombre)
    except IntegrityError:
        messages.error(request, f"Ya existe un perfil llamado '{nombre}'.")
    else:
        messages.success(request, f"Perfil '{copia.nombre}' creado a partir de '{perfil.nombre}'.")
    return redirect("perfiles")


@require_POST
def perfil_borrar(request: HttpRequest, pk: int) -> HttpResponse:
    """Borra un perfil con todo lo suyo. Ni el activo ni el último."""
    perfil = get_object_or_404(Perfil, pk=pk)
    if perfil.activo:
        messages.error(request, "No se puede borrar el perfil activo. Activa otro primero.")
        return redirect("perfiles")
    if Perfil.objects.count() == 1:
        messages.error(request, "Tiene que quedar al menos un perfil.")
        return redirect("perfiles")

    if request.session.get(CLAVE_SESION_PERFIL) == perfil.pk:
        del request.session[CLAVE_SESION_PERFIL]
    nombre = perfil.nombre
    perfil.delete()
    messages.success(request, f"Perfil '{nombre}' borrado, con su historial y sus ajustes.")
    return redirect("perfiles")


# --- Despliegue y control del servicio ---------------------------------------


@require_POST
def desplegar(request: HttpRequest) -> HttpResponse:
    """Exporta la configuración y reinicia el agente para que la tome.

    Es el único camino por el que lo guardado llega a producción. Si la
    exportación falla no se escribe nada ni se reinicia: más vale seguir con la
    configuración anterior que dejar al agente a medias.
    """
    autor = request.user if request.user.is_authenticated else None
    try:
        despliegue = exportar(django_settings.DATA_DIR, autor)
    except ErrorDeExportacion as e:
        Despliegue.objects.create(autor=autor, resultado=Despliegue.Resultado.ERROR, detalle=str(e))
        messages.error(request, f"No se ha desplegado nada. {e}")
        return redirect("panel")

    if request.POST.get("reiniciar") == "no":
        messages.success(request, "Configuración exportada. El agente la tomará al reiniciarse.")
        return redirect("panel")

    try:
        control.reiniciar()
    except control.ErrorDeControl as e:
        despliegue.detalle = f"Exportado, pero no se pudo reiniciar: {e}"
        despliegue.save(update_fields=["detalle"])
        messages.warning(request, despliegue.detalle)
        return redirect("panel")

    despliegue.resultado = Despliegue.Resultado.REINICIADO
    despliegue.save(update_fields=["resultado"])
    messages.success(
        request,
        "Configuración exportada y agente reiniciándose. Tarda unos veinte segundos "
        "en volver a conversar.",
    )
    return redirect("panel")


@require_POST
def servicio(request: HttpRequest, accion: str) -> HttpResponse:
    """Arranca, para o reinicia el agente, o lanza la reindexación."""
    acciones = {
        "arrancar": control.arrancar,
        "parar": control.parar,
        "reiniciar": control.reiniciar,
    }
    # La reindexación tiene su propia función porque además de lanzar la unidad
    # deja constancia de con qué corpus se lanzó, que es lo que luego permite
    # avisar de que el índice se ha quedado viejo.
    if accion == "ingesta":
        _lanzar_reindexado(request)
        # El destino se compara contra un valor fijo en vez de pasarse a
        # `redirect`: un nombre de vista venido del POST sería una redirección
        # abierta en cuanto alguien escribiera una URL entera en ese campo.
        vuelve_a_conocimiento = request.POST.get("volver") == "conocimiento"
        return redirect("conocimiento" if vuelve_a_conocimiento else "panel")

    if accion not in acciones:
        messages.error(request, "Acción desconocida.")
        return redirect("panel")

    try:
        acciones[accion]()
    except control.ErrorDeControl as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Orden '{accion}' enviada a systemd.")
    return redirect("panel")


# --- Logs --------------------------------------------------------------------


def logs(request: HttpRequest) -> HttpResponse:
    """Página que sigue el log del agente en vivo."""
    return render(
        request,
        "panel/logs.html",
        {"ruta": django_settings.RUTA_LOG_AGENTE, "ultimas": tailer.ultimas_lineas()},
    )


def logs_stream(request: HttpRequest) -> StreamingHttpResponse:
    """Envía las líneas nuevas del log según aparecen.

    Se usan eventos del servidor (SSE) y no websockets: no hacen falta Channels
    ni Redis, va sobre HTTP normal y son unas pocas decenas de líneas de código.
    """
    respuesta = StreamingHttpResponse(tailer.eventos(), content_type="text/event-stream")
    respuesta["Cache-Control"] = "no-cache"
    # Sin esto, cualquier proxy intermedio bufferiza y el "en vivo" deja de serlo.
    respuesta["X-Accel-Buffering"] = "no"
    return respuesta


# --- Bitácora ----------------------------------------------------------------


def despliegues(request: HttpRequest) -> HttpResponse:
    """Historial de lo que se le ha enviado al agente."""
    return render(request, "panel/despliegues.html", {"despliegues": Despliegue.objects.all()[:50]})


def despliegue_detalle(request: HttpRequest, pk: int) -> HttpResponse:
    """Las instantáneas exactas que se enviaron en un despliegue."""
    import json

    despliegue = get_object_or_404(Despliegue, pk=pk)
    contexto: dict[str, Any] = {
        "despliegue": despliegue,
        "settings_json": json.dumps(despliegue.instantanea_settings, indent=2, ensure_ascii=False),
        "runtime_json": json.dumps(despliegue.instantanea_runtime, indent=2, ensure_ascii=False),
    }
    return render(request, "panel/despliegue_detalle.html", contexto)
