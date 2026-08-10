"""Traduce lo guardado en el panel a los dos ficheros que lee el agente.

Es la frontera entre los dos procesos, y hace tres cosas que no son negociables:

1. **Valida antes de escribir.** El snapshot de ajustes se pasa por `Settings`
   antes de tocar el disco, así que un valor imposible falla aquí, con el
   mensaje de pydantic delante, y no cuarenta segundos después en el arranque
   del agente y en otro contenedor.
2. **Usa el mismo modelo que el agente.** `RuntimeConfig` se importa de
   `voice_agent_core`, de modo que el contrato no puede derivar: si el agente
   añade un campo, aquí falla al compilar, no en producción.
3. **Escribe de forma atómica.** Un fichero a medias tras un corte de corriente
   dejaría la placa sin agente.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from voice_agent_core.config import CAMPOS_PROTEGIDOS, Settings
from voice_agent_core.runtime import (
    HookConfig,
    MCPServerConfig,
    PromptConfig,
    RuntimeConfig,
    ToolConfig,
)
from voice_agent_core.rutas import (
    escribir_json_atomico,
    ruta_runtime,
    ruta_snapshot_settings,
    ruta_tareas,
)
from voice_agent_core.tareas import TareaProgramada as TareaProgramadaConfig
from voice_agent_core.tareas import TareasConfig
from voice_agent_panel.models import (
    AjusteAgente,
    Despliegue,
    Herramienta,
    Hook,
    Perfil,
    ServidorMCP,
    TareaProgramada,
    VersionPrompt,
)


class ErrorDeExportacion(Exception):
    """La configuración guardada no se puede mandar al agente tal cual está."""


def construir_snapshot_settings() -> dict[str, Any]:
    """Reúne los ajustes sobreescritos y comprueba que `Settings` los acepta.

    Returns:
        El diccionario que se escribirá en `settings.json`.

    Raises:
        ErrorDeExportacion: Si algún valor no valida.
    """
    conocidos = Settings.model_fields
    snapshot: dict[str, Any] = {}
    # Solo los ajustes del perfil activo. Sin perfil activo la queryset queda
    # vacía y se exporta "sin overrides", que es la degradación segura.
    for ajuste in AjusteAgente.objects.filter(perfil=Perfil.activo_o_none()):
        if ajuste.clave in CAMPOS_PROTEGIDOS or ajuste.clave not in conocidos:
            # Puede pasar si se renombró un campo entre versiones. Se ignora en
            # silencio aquí porque la fuente del agente también lo ignoraría.
            continue
        try:
            snapshot[ajuste.clave] = json.loads(ajuste.valor)
        except json.JSONDecodeError as e:
            raise ErrorDeExportacion(f"El ajuste '{ajuste.clave}' no es JSON válido: {e}") from e

    try:
        # `_env_file=None` para validar solo lo que se va a mandar, sin que el
        # .env de esta máquina complete huecos y tape un error.
        Settings(_env_file=None, **snapshot)  # type: ignore[call-arg]
    except ValueError as e:
        raise ErrorDeExportacion(f"Los ajustes no son válidos:\n{e}") from e

    return snapshot


def construir_runtime() -> RuntimeConfig:
    """Compone la configuración de prompt, herramientas, MCP y hooks.

    Raises:
        ErrorDeExportacion: Si algo no encaja con el modelo que espera el agente.
    """
    perfil = Perfil.activo_o_none()
    version = VersionPrompt.activa_de(perfil) if perfil else None
    prompt = (
        PromptConfig(
            prompt_sistema=version.prompt_sistema,
            alma=version.alma,
            saludo_inicial=version.saludo_inicial,
            muletillas=version.muletillas,
        )
        if version
        else PromptConfig()
    )

    # Los servidores MCP y los hooks se exportan todos, con `habilitado` según
    # la selección del perfil activo: el formato del fichero no cambia y el
    # agente sigue sin saber qué es un perfil.
    mcp_activos = set(perfil.mcp_habilitados.values_list("pk", flat=True)) if perfil else set()
    hooks_activos = set(perfil.hooks_habilitados.values_list("pk", flat=True)) if perfil else set()

    try:
        return RuntimeConfig(
            generado_en=datetime.now(UTC),
            perfil=perfil.nombre if perfil else "",
            prompt=prompt,
            herramientas=[
                ToolConfig(nombre=h.nombre, habilitada=h.habilitada)
                for h in Herramienta.objects.filter(perfil=perfil)
            ],
            mcp=[
                MCPServerConfig(
                    nombre=s.nombre,
                    habilitado=s.pk in mcp_activos,
                    transporte=s.transporte,
                    comando=s.comando or None,
                    argumentos=list(s.argumentos or []),
                    entorno=dict(s.entorno or {}),
                    url=s.url or None,
                    cabeceras=dict(s.cabeceras or {}),
                    herramientas_permitidas=list(s.herramientas_permitidas or []),
                    timeout_secs=s.timeout_secs,
                )
                for s in ServidorMCP.objects.all()
            ],
            hooks=[
                HookConfig(
                    nombre=h.nombre,
                    habilitado=h.pk in hooks_activos,
                    orden=h.orden,
                    evento=h.evento,
                    accion=h.accion,
                    comando=list(h.comando or []),
                    entorno=dict(h.entorno or {}),
                    timeout_secs=h.timeout_secs,
                    bloqueante=h.bloqueante,
                    patron=h.patron,
                    reemplazo=h.reemplazo,
                )
                for h in Hook.objects.all()
            ],
        )
    except ValueError as e:
        raise ErrorDeExportacion(f"La configuración no es válida:\n{e}") from e


def construir_tareas() -> TareasConfig:
    """Compone las tareas programadas con el modelo que espera el agente.

    Van todas, deshabilitadas incluidas: el formato no depende de qué esté
    encendido y el planificador filtra por `habilitada`, igual que el agente
    hace con los servidores MCP.

    Raises:
        ErrorDeExportacion: Si alguna tarea guardada no valida (no debería
            ocurrir: el formulario valida con el mismo modelo).
    """
    try:
        return TareasConfig(
            generado_en=datetime.now(UTC),
            tareas=[
                TareaProgramadaConfig(
                    id=t.nombre,
                    nombre=t.titulo,
                    habilitada=t.habilitada,
                    tipo=t.tipo,
                    cron=t.cron,
                    mision=t.mision,
                    guardar_respuestas=t.guardar_respuestas,
                    contacto_nombre=t.contacto_nombre,
                    contacto_numero=t.contacto_numero,
                )
                for t in TareaProgramada.objects.all()
            ],
        )
    except ValueError as e:
        raise ErrorDeExportacion(f"Las tareas programadas no son válidas:\n{e}") from e


def exportar_tareas(data_dir: Path) -> None:
    """Escribe `tareas.json`, que el agente recoge en caliente.

    Va aparte de `exportar` porque las tareas no necesitan reinicio: las vistas
    lo llaman en cada alta, edición o borrado y el planificador del agente ve
    el mtime nuevo en su siguiente vuelta.
    """
    escribir_json_atomico(ruta_tareas(data_dir), construir_tareas().model_dump(mode="json"))


def exportar(data_dir: Path, autor: Any = None) -> Despliegue:
    """Escribe los dos ficheros y deja constancia de lo enviado.

    Args:
        data_dir: La raíz de datos compartida con el agente.
        autor: El usuario que lo pidió, para la bitácora.

    Returns:
        El registro del despliegue.

    Raises:
        ErrorDeExportacion: Si la configuración no valida. En ese caso **no se
            escribe nada**: más vale no desplegar que dejar al agente con medio
            fichero.
    """
    snapshot = construir_snapshot_settings()
    runtime = construir_runtime()
    tareas = construir_tareas()

    escribir_json_atomico(ruta_snapshot_settings(data_dir), snapshot)
    escribir_json_atomico(ruta_runtime(data_dir), runtime.model_dump(mode="json"))
    # Un despliegue completo refresca también las tareas: así un `tareas.json`
    # borrado a mano se regenera por el mismo camino que todo lo demás.
    escribir_json_atomico(ruta_tareas(data_dir), tareas.model_dump(mode="json"))

    return Despliegue.objects.create(
        autor=autor if autor and getattr(autor, "is_authenticated", False) else None,
        resultado=Despliegue.Resultado.EXPORTADO,
        instantanea_settings=snapshot,
        instantanea_runtime=runtime.model_dump(mode="json"),
    )


def herramientas_citadas_en_el_prompt(desactivadas: set[str]) -> list[str]:
    """Devuelve las herramientas apagadas que el prompt activo sigue anunciando.

    Es una trampa real y silenciosa: si se desactiva `buscar_en_documentos` pero
    el prompt sigue diciendo "tienes una herramienta para consultar tu base de
    conocimiento", el modelo afirmará tan tranquilo que la ha consultado. El
    panel avisa en vez de dejar que se descubra conversando.
    """
    perfil = Perfil.activo_o_none()
    version = VersionPrompt.activa_de(perfil) if perfil else None
    if not version or not desactivadas:
        return []
    texto = f"{version.prompt_sistema}\n{version.alma}".lower()
    return sorted(n for n in desactivadas if n.lower() in texto)
