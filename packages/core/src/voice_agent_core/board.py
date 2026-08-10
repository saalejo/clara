"""Lectura del estado del hardware de la placa.

Todo sale de `/proc` y `/sys`, que son ficheros de texto del núcleo: sin
dependencias, sin permisos especiales y sin coste apreciable.

Vive en el paquete `core` y no junto a la herramienta que lo usa porque hay dos
consumidores: la herramienta `estado_del_sistema`, que se lo cuenta al usuario
por voz, y el panel, que lo pinta en su portada. Dejarlo en `voice_agent.tools`
obligaría al panel a importar Pipecat solo para leer un fichero de texto.
"""

from __future__ import annotations

from pathlib import Path

RUTA_TERMICA = Path("/sys/class/thermal")
RUTA_MEMORIA = Path("/proc/meminfo")
RUTA_CARGA = Path("/proc/loadavg")
RUTA_UPTIME = Path("/proc/uptime")


def temperaturas() -> dict[str, float]:
    """Lee las zonas térmicas del núcleo, en grados centígrados."""
    lecturas: dict[str, float] = {}
    if not RUTA_TERMICA.is_dir():
        return lecturas
    for zona in sorted(RUTA_TERMICA.glob("thermal_zone*")):
        try:
            nombre = (zona / "type").read_text().strip()
            # El núcleo expone la temperatura en milésimas de grado.
            lecturas[nombre] = int((zona / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return lecturas


def memoria() -> dict[str, float]:
    """Lee el uso de memoria, en gigabytes."""
    if not RUTA_MEMORIA.is_file():
        return {}
    campos: dict[str, int] = {}
    for linea in RUTA_MEMORIA.read_text().splitlines():
        clave, _, resto = linea.partition(":")
        if clave in ("MemTotal", "MemAvailable"):
            campos[clave] = int(resto.strip().split()[0])  # viene en kibibytes
    if "MemTotal" not in campos:
        return {}
    total = campos["MemTotal"] / 1024 / 1024
    disponible = campos.get("MemAvailable", 0) / 1024 / 1024
    return {
        "total_gb": round(total, 2),
        "disponible_gb": round(disponible, 2),
        "usada_gb": round(total - disponible, 2),
    }


def carga_media() -> dict[str, str]:
    """Devuelve la carga media a 1, 5 y 15 minutos."""
    valores = RUTA_CARGA.read_text().split()[:3] if RUTA_CARGA.is_file() else ["?", "?", "?"]
    return {"1_min": valores[0], "5_min": valores[1], "15_min": valores[2]}


def uptime_legible() -> str:
    """Devuelve el tiempo encendido en un formato que se pueda leer en voz alta."""
    if not RUTA_UPTIME.is_file():
        return "desconocido"
    segundos = float(RUTA_UPTIME.read_text().split()[0])
    dias, resto = divmod(int(segundos), 86400)
    horas, resto = divmod(resto, 3600)
    minutos = resto // 60
    partes = []
    if dias:
        partes.append(f"{dias} día{'s' if dias != 1 else ''}")
    if horas:
        partes.append(f"{horas} hora{'s' if horas != 1 else ''}")
    partes.append(f"{minutos} minuto{'s' if minutos != 1 else ''}")
    return ", ".join(partes)


def estado_placa() -> dict[str, object]:
    """Reúne todas las lecturas en un solo diccionario."""
    return {
        "temperaturas_celsius": {k: round(v, 1) for k, v in temperaturas().items()},
        "memoria": memoria(),
        "carga_media": carga_media(),
        "tiempo_encendida": uptime_legible(),
    }
