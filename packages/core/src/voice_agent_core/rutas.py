"""Rutas de los ficheros que el panel y el agente se intercambian.

El panel y el agente corren en **contenedores distintos** y no se hablan por red:
se comunican dejando ficheros JSON en el volumen de datos, que ambos montan.
Este módulo es el único sitio donde se decide cómo se llaman y dónde están, para
que un cambio de nombre no se convierta en una cacería.

    <DATA_DIR>/config/settings.json          panel -> agente (campos de Settings)
    <DATA_DIR>/config/runtime.json           panel -> agente (prompt, alma, tools, mcp, hooks)
    <DATA_DIR>/config/tareas.json            panel -> agente (tareas programadas)
    <DATA_DIR>/config/estado_arranque.json   agente -> panel (qué cargó de verdad)
    <DATA_DIR>/logs/agente.log               agente -> panel (log en vivo)
    <DATA_DIR>/tareas/resultados/<id>/       agente -> panel (respuestas de cuestionarios)
    <DATA_DIR>/tareas/bitacora.jsonl         agente -> panel (bitácora de ejecuciones)
    <DATA_DIR>/calidad/solicitud.json        panel -> runner (qué escenarios ensayar)
    <DATA_DIR>/calidad/lote.json             runner -> panel (progreso del lote)
    <DATA_DIR>/calidad/resultados/<id>.json  runner -> panel (expediente por ejecución)
    <DATA_DIR>/calidad/sandbox/<id>/         runner (data_dir aislado de cada ensayo)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SUBDIR_CONFIG = "config"
SUBDIR_LOGS = "logs"
SUBDIR_TAREAS = "tareas"
SUBDIR_CALIDAD = "calidad"

NOMBRE_SOLICITUD_CALIDAD = "solicitud.json"
NOMBRE_LOTE_CALIDAD = "lote.json"

NOMBRE_SNAPSHOT_SETTINGS = "settings.json"
NOMBRE_RUNTIME = "runtime.json"
NOMBRE_TAREAS = "tareas.json"
NOMBRE_ESTADO = "estado_arranque.json"
NOMBRE_LOG_AGENTE = "agente.log"
NOMBRE_BITACORA_TAREAS = "bitacora.jsonl"

#: Variable de entorno para apuntar la instantánea a otro sitio. La usan los
#: tests para aislarse (ver `tests/conftest.py`) y sirve de escape en desarrollo.
VAR_ENTORNO_SNAPSHOT = "VOICE_AGENT_PANEL_CONFIG"


def dir_config(data_dir: Path) -> Path:
    """Carpeta donde viven los ficheros de intercambio."""
    return data_dir / SUBDIR_CONFIG


def ruta_snapshot_settings(data_dir: Path) -> Path:
    """Instantánea de los campos de `Settings` que gobierna el panel."""
    return dir_config(data_dir) / NOMBRE_SNAPSHOT_SETTINGS


def ruta_runtime(data_dir: Path) -> Path:
    """Configuración de prompt, alma, herramientas, MCP y hooks."""
    return dir_config(data_dir) / NOMBRE_RUNTIME


def ruta_tareas(data_dir: Path) -> Path:
    """Tareas programadas que el panel exporta y el agente recarga en caliente."""
    return dir_config(data_dir) / NOMBRE_TAREAS


def dir_tareas(data_dir: Path) -> Path:
    """Carpeta de trabajo de las tareas: resultados y bitácora."""
    return data_dir / SUBDIR_TAREAS


def dir_resultados_tareas(data_dir: Path) -> Path:
    """Respuestas de cuestionarios, una subcarpeta por id de tarea."""
    return dir_tareas(data_dir) / "resultados"


def ruta_bitacora_tareas(data_dir: Path) -> Path:
    """Bitácora de ejecuciones del planificador, una línea JSON por disparo."""
    return dir_tareas(data_dir) / NOMBRE_BITACORA_TAREAS


def dir_evaluaciones(data_dir: Path) -> Path:
    """Carpeta de las evaluaciones clínicas: alertas, resúmenes y trazas."""
    return data_dir / "evaluaciones"


def dir_alertas(data_dir: Path) -> Path:
    """Alertas de escalamiento, un JSON por alerta registrada."""
    return dir_evaluaciones(data_dir) / "alertas"


def dir_resumenes(data_dir: Path) -> Path:
    """Resúmenes estructurados de llamada, un JSON por llamada terminada."""
    return dir_evaluaciones(data_dir) / "resumenes"


def dir_trazas(data_dir: Path) -> Path:
    """Trazas de consultas al RAG, un JSONL por llamada."""
    return dir_evaluaciones(data_dir) / "trazas"


def ruta_historial(data_dir: Path) -> Path:
    """Base SQLite del historial de pacientes por número de teléfono."""
    return dir_evaluaciones(data_dir) / "historial.sqlite3"


def dir_calidad(data_dir: Path) -> Path:
    """Carpeta de las pruebas de calidad adversarias."""
    return data_dir / SUBDIR_CALIDAD


def ruta_solicitud_calidad(data_dir: Path) -> Path:
    """Petición del panel al runner: qué escenarios ensayar."""
    return dir_calidad(data_dir) / NOMBRE_SOLICITUD_CALIDAD


def ruta_lote_calidad(data_dir: Path) -> Path:
    """Progreso del lote que el runner publica para el panel."""
    return dir_calidad(data_dir) / NOMBRE_LOTE_CALIDAD


def dir_resultados_calidad(data_dir: Path) -> Path:
    """Expedientes de las ejecuciones, un JSON por ejecución."""
    return dir_calidad(data_dir) / "resultados"


def dir_sandbox_calidad(data_dir: Path) -> Path:
    """Raíz de los `data_dir` aislados de cada ensayo.

    Cada ejecución corre con su propio `data_dir` bajo aquí, para que las
    alertas y resúmenes de prueba no se mezclen con los de pacientes reales en
    las páginas de Evaluaciones y Pacientes del panel.
    """
    return dir_calidad(data_dir) / "sandbox"


def ruta_estado(data_dir: Path) -> Path:
    """Lo que el agente publica sobre sí mismo tras arrancar."""
    return dir_config(data_dir) / NOMBRE_ESTADO


def ruta_log_agente(data_dir: Path) -> Path:
    """Copia en fichero del log del agente, que el panel sigue en vivo."""
    return data_dir / SUBDIR_LOGS / NOMBRE_LOG_AGENTE


def ruta_snapshot_desde_entorno() -> Path:
    """Resuelve la ruta de la instantánea leyendo solo el entorno.

    No puede recibir un `Settings` porque se la llama **mientras se construye**
    uno: es la fuente de configuración la que necesita saber de dónde leer. Que
    la ruta salga de `os.environ` y no de un campo tiene una ventaja que no es
    accidental: garantiza por construcción que `data_dir` nunca puede venir de
    la propia instantánea, que es justo la trampa de rutas que rompió el
    contenedor en su día.

    Returns:
        La ruta del fichero, exista o no.
    """
    explicita = os.environ.get(VAR_ENTORNO_SNAPSHOT)
    if explicita:
        return Path(explicita)
    return ruta_snapshot_settings(Path(os.environ.get("DATA_DIR", "data")))


def escribir_json_atomico(ruta: Path, datos: Any) -> None:
    """Escribe un JSON de forma que nunca se lea a medias.

    Se escribe en un temporal del mismo directorio —tiene que ser el mismo
    sistema de ficheros para que `os.replace` sea atómico— se fuerza a disco y
    se renombra encima. Sin esto, un corte de corriente en el momento justo
    dejaría un fichero truncado y la placa arrancaría sin agente.

    Args:
        ruta: Destino final.
        datos: Cualquier cosa serializable a JSON.
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(dir=ruta.parent, prefix=f".{ruta.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as fichero:
            json.dump(datos, fichero, ensure_ascii=False, indent=2)
            fichero.flush()
            os.fsync(fichero.fileno())
        # `mkstemp` crea con permisos 600, que es lo correcto para un temporal
        # pero no para esto: son ficheros de intercambio entre dos contenedores
        # que no tienen por qué correr con el mismo usuario dentro. Hoy funciona
        # porque ambos acaban siendo el mismo uid en el anfitrión, pero basta
        # cambiar el mapeo de uno para que el otro deje de poder leerlos, con un
        # "permission denied" difícil de relacionar con esto. No llevan secretos:
        # las claves están en CAMPOS_PROTEGIDOS y no salen del .env.
        os.chmod(temporal, 0o644)
        os.replace(temporal, ruta)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise
