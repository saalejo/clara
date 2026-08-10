"""La separación de dependencias del demonio de botones.

Tercer espejo de `test_core_liviano.py`, con la misma lógica: lo que sostiene la
arquitectura no es la buena voluntad, es un test.

El demonio corre **nativo** en la placa y su gracia es ser diminuto: lee un
device de `/dev/input` con `struct`, lanza `amixer` y `aplay` por subprocess,
habla con systemd por el bus de sesión y con el puente de telefonía por HTTP
sobre un socket unix. Nada de eso necesita Pipecat, ni chromadb, ni Django.

Y en el otro sentido: **no puede hablar D-Bus con oFono**. La telefonía se
consulta por el socket del puente, igual que hace el agente. Si `dbus_fast`
apareciera aquí sería la señal de que alguien ha empezado a duplicar el puente
dentro del mando físico.
"""

from __future__ import annotations

import subprocess
import sys

# Los módulos del paquete que tienen que poder importarse sin pagar peso. Se
# listan a mano en lugar de descubrirlos para que añadir un módulo nuevo obligue
# a pasar por aquí y a pensar qué arrastra.
MODULOS = (
    "acciones",
    "config",
    "demonio",
    "entrada",
    "gestos",
    "mezclador",
    "pitidos",
    "servicios",
    "telefonia",
)

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


def test_los_botones_no_arrastran_las_dependencias_del_agente() -> None:
    colados = _modulos_cargados(
        importaciones="\n".join(f"import voice_agent_botones.{m}" for m in MODULOS),
        prohibidos=("pipecat", "chromadb", "fastembed", "torch", "onnxruntime", "django"),
    )
    assert not colados, (
        f"Importar voice_agent_botones ha cargado {colados}. El demonio corre NATIVO y su "
        "gracia es ser diminuto: arrancar el mando físico no puede costar lo que arrancar "
        "el agente."
    )


def test_los_botones_no_hablan_dbus_con_ofono() -> None:
    colados = _modulos_cargados(
        importaciones="\n".join(f"import voice_agent_botones.{m}" for m in MODULOS),
        prohibidos=("dbus_fast", "starlette", "uvicorn"),
    )
    assert not colados, (
        f"El demonio de botones ha cargado {colados}. La telefonía se consulta por el socket "
        "del puente, como hace el agente; hablar D-Bus con oFono desde aquí sería duplicar "
        "packages/telefonia."
    )
