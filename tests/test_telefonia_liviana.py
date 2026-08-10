"""La separación de dependencias entre el puente y el agente.

Espejo de `test_core_liviano.py`, y por el mismo motivo: lo que sostiene la
arquitectura no es la buena voluntad, es un test.

* El **puente** corre nativo en la placa. Si importara Pipecat o chromadb,
  arrancarlo cargaría 1,1 GB de dependencias para hablar con D-Bus.
* El **agente** vive en un contenedor. Si importara `dbus_fast`, esa librería
  entraría en su imagen — y con ella la tentación de hablar con D-Bus desde
  dentro del contenedor, que es justo lo que este diseño evita (autenticación
  EXTERNAL, `--userns=keep-id`, dos buses...).

Se comprueba en subprocesos limpios porque el resto de la batería sí importa
Pipecat, así que mirar `sys.modules` desde aquí no diría nada.
"""

from __future__ import annotations

import subprocess
import sys

PROGRAMA = """
import sys

{importaciones}

prohibidos = {prohibidos}
print(",".join(sorted({{m.split(".")[0] for m in sys.modules}} & set(prohibidos))))
"""


def _modulos_cargados(importaciones: str, prohibidos: tuple[str, ...]) -> list[str]:
    resultado = subprocess.run(
        [
            sys.executable,
            "-c",
            PROGRAMA.format(importaciones=importaciones, prohibidos=prohibidos),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [m for m in resultado.stdout.strip().split(",") if m]


def test_el_puente_no_arrastra_las_dependencias_del_agente() -> None:
    colados = _modulos_cargados(
        importaciones="\n".join(
            f"import voice_agent_telefonia.{m}"
            for m in (
                "api",
                "bus",
                "contactos",
                "eventos",
                "llamadas",
                "normaliza",
                "pbap",
                "preferencias",
                "servicio",
                "vcard",
            )
        ),
        prohibidos=("pipecat", "chromadb", "fastembed", "torch", "onnxruntime", "django"),
    )
    assert not colados, (
        f"Importar voice_agent_telefonia ha cargado {colados}. El puente corre NATIVO en la "
        "placa: no puede depender de lo que solo tiene sentido dentro de la imagen del agente."
    )


def test_las_herramientas_del_agente_no_arrastran_dbus() -> None:
    colados = _modulos_cargados(
        importaciones="import voice_agent.telefonia\nimport voice_agent.tools.telefono",
        prohibidos=("dbus_fast", "starlette", "uvicorn"),
    )
    assert not colados, (
        f"El cliente de telefonía del agente ha cargado {colados}. Toda la parte de D-Bus tiene "
        "que quedarse en packages/telefonia: el agente solo habla HTTP por un socket unix."
    )
