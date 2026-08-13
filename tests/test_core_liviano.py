"""El paquete `core` no puede arrastrar las dependencias pesadas del agente.

Es la prueba que sostiene toda la separación en dos imágenes. El panel importa
`voice_agent_core` para generar sus formularios a partir de `Settings`; si
alguien añade ahí un import de Pipecat o de chromadb —aunque sea solo para una
anotación de tipo— la imagen del panel pasa de 200 MB a 1,3 GB y su proceso web
se come cientos de megabytes de RSS para siempre, en una placa que ya tiene 2 GB
comprometidos con el agente.

Se comprueba en un subproceso limpio porque el resto de la batería sí importa
Pipecat, así que mirar `sys.modules` desde aquí no diría nada.

El segundo test es más fino y legitima que `voice_agent_core.systemd` exista:
`jeepney` es una dependencia de `core`, pero **solo la paga quien pide ese módulo**.
Si un día alguien lo importara desde `config` o `runtime` —por una anotación de
tipo, típicamente— el agente cargaría un cliente de D-Bus en cada arranque para
nada, y con él la tentación de hablar con D-Bus desde dentro del contenedor, que
es justo lo que este diseño evita.
"""

from __future__ import annotations

import subprocess
import sys

MODULOS_PESADOS = ("pipecat", "chromadb", "fastembed", "torch", "onnxruntime", "django")

PROGRAMA = """
import sys

import voice_agent_core.board
import voice_agent_core.calidad
import voice_agent_core.cobertura
import voice_agent_core.config
import voice_agent_core.corpus
import voice_agent_core.cron
import voice_agent_core.estado
import voice_agent_core.expediente
import voice_agent_core.historial
import voice_agent_core.limitador
import voice_agent_core.misiones
import voice_agent_core.prompts
import voice_agent_core.runtime
import voice_agent_core.rutas
import voice_agent_core.tareas

pesados = {pesados}
print(",".join(sorted({{m.split(".")[0] for m in sys.modules}} & set(pesados))))
"""


def test_core_no_arrastra_dependencias_pesadas() -> None:
    resultado = subprocess.run(
        [sys.executable, "-c", PROGRAMA.format(pesados=MODULOS_PESADOS)],
        capture_output=True,
        text=True,
        check=True,
    )
    colados = [m for m in resultado.stdout.strip().split(",") if m]
    assert not colados, (
        f"Importar voice_agent_core ha cargado {colados}. Eso rompe la imagen ligera del panel: "
        "mueve ese import al paquete voice_agent, o hazlo perezoso dentro de la función."
    )


def test_solo_paga_jeepney_quien_pide_el_modulo_de_systemd() -> None:
    """`jeepney` es dependencia de `core`, pero no la carga quien no la necesita.

    Es lo que hace legítimo que `voice_agent_core.systemd` viva aquí en lugar de
    estar duplicado entre el panel y el mando físico.
    """
    resultado = subprocess.run(
        [sys.executable, "-c", PROGRAMA.format(pesados=("jeepney",))],
        capture_output=True,
        text=True,
        check=True,
    )
    colados = [m for m in resultado.stdout.strip().split(",") if m]
    assert not colados, (
        "Importar los módulos base de voice_agent_core ha cargado jeepney. Ese import tiene que "
        "quedarse dentro de voice_agent_core.systemd: si no, el agente carga un cliente de D-Bus "
        "en cada arranque para nada."
    )


def test_el_modulo_de_systemd_si_carga_jeepney() -> None:
    """Comprueba que el test anterior mide algo.

    Sin esto, un `PROGRAMA` que dejara de importar nada seguiría pasando en verde
    y el guarda no guardaría nada.
    """
    resultado = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, voice_agent_core.systemd; print('jeepney' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert resultado.stdout.strip() == "True"
