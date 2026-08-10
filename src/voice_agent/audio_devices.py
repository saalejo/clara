"""Descubrimiento y diagnóstico de los dispositivos de audio del sistema.

Pipecat pide el dispositivo a PortAudio por **índice numérico**, pero los
índices no son estables: dependen del orden de enumeración y cambian si se
conecta o desconecta hardware USB. Este módulo permite configurar el
dispositivo por **nombre** (`AUDIO_INPUT_DEVICE=default`) y resolverlo a índice
en tiempo de arranque.

Además expone un comprobador de extremo a extremo::

    make audio-check

que graba unos segundos por el dispositivo de entrada configurado y los
reproduce por el de salida, usando exactamente la misma frecuencia de muestreo
y número de canales que usará el agente. Si esto funciona, el camino de audio
es correcto; si falla, no tiene sentido seguir depurando más arriba.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import math
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pyaudio
from loguru import logger

from voice_agent.logging import setup_logging, silence_alsa_warnings, suppressed_stderr
from voice_agent_core.config import Settings, get_settings


@dataclass(frozen=True)
class AudioDevice:
    """Un dispositivo de audio tal y como lo ve PortAudio."""

    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float

    @property
    def can_record(self) -> bool:
        """Indica si el dispositivo puede capturar audio."""
        return self.max_input_channels > 0

    @property
    def can_play(self) -> bool:
        """Indica si el dispositivo puede reproducir audio."""
        return self.max_output_channels > 0

    def describe(self) -> str:
        """Devuelve una línea legible con las capacidades del dispositivo."""
        caps = []
        if self.can_record:
            caps.append(f"entrada x{self.max_input_channels}")
        if self.can_play:
            caps.append(f"salida x{self.max_output_channels}")
        return f"[{self.index:2d}] {self.name:<32} {', '.join(caps):<24} {self.default_sample_rate:.0f} Hz"


class AudioDeviceError(RuntimeError):
    """No se pudo resolver un dispositivo de audio con la configuración dada."""


#: De dónde salen las tarjetas presentes. `/proc/asound` no está sujeto a
#: espacios de nombres, así que dentro del contenedor refleja las tarjetas del
#: anfitrión: es lo que permite detectar la conexión de una tarjeta USB sin
#: udev ni acceso privilegiado.
RUTA_TARJETAS_ALSA = Path("/proc/asound/cards")

#: Cada cuánto se sondea `/proc/asound` esperando o vigilando la tarjeta. Leer
#: un fichero de procfs cuesta microsegundos; dos segundos es de sobra para que
#: conectar la tarjeta se sienta inmediato sin gastar nada.
INTERVALO_SONDEO_SECS = 2.0


def extraer_tarjetas(contenido: str) -> frozenset[str]:
    """Saca los identificadores de tarjeta de un `/proc/asound/cards`.

    El formato es una línea por tarjeta, `` 0 [Device         ]: USB-Audio...``;
    sin tarjetas, el kernel escribe ``--- no soundcards ---``, que no casa con
    el patrón y produce el conjunto vacío sin ningún caso especial.

    Args:
        contenido: El texto del fichero.

    Returns:
        Los ids entre corchetes, sin el relleno de espacios.
    """
    return frozenset(re.findall(r"^\s*\d+\s+\[(\S+)", contenido, re.MULTILINE))


def tarjetas_alsa() -> frozenset[str]:
    """Devuelve los ids de las tarjetas ALSA presentes ahora mismo.

    Se usa el id (`Device`, `Dummy`...) y no el índice porque el id identifica
    al hardware: si la tarjeta se desconecta y vuelve, puede recibir otro
    índice, pero el conjunto de ids queda igual y no hay reinicio gratuito.
    """
    try:
        return extraer_tarjetas(RUTA_TARJETAS_ALSA.read_text())
    except OSError:
        # Sin /proc/asound no hay ni driver de sonido: como no tener tarjetas.
        return frozenset()


async def esperar_dispositivos(
    settings: Settings, *, intervalo_secs: float = INTERVALO_SONDEO_SECS
) -> frozenset[str]:
    """Espera a que los dispositivos configurados existan y se puedan resolver.

    Es lo que hace al agente tolerante a arrancar sin tarjeta de sonido: en vez
    de morir y dejar que systemd lo reintente —tirando en cada vuelta los
    cuarenta segundos de carga de modelos, y llevándose por delante la
    telefonía, que no necesita tarjeta—, el audio de la sala espera aquí a que
    la tarjeta aparezca.

    El sondeo es en dos pasos a propósito: primero `/proc/asound`, que es
    gratis, y solo cuando hay alguna tarjeta se paga el arranque de PortAudio
    para comprobar que los dispositivos configurados de verdad se resuelven
    (la tarjeta puede no ser la que espera `/etc/asound.conf`).

    Args:
        settings: Configuración del agente.
        intervalo_secs: Cada cuánto se sondea.

    Returns:
        Los ids de las tarjetas presentes en el momento en que los dispositivos
        resolvieron, para que quien llama pueda vigilar después si cambian.
    """
    avisado = False
    while True:
        tarjetas = tarjetas_alsa()
        if tarjetas:
            try:
                # En un hilo aparte: arrancar PortAudio sondea todos sus
                # back-ends y puede tardar; el bucle de eventos —del que ya
                # cuelga la telefonía— no puede congelarse mientras tanto.
                await asyncio.to_thread(resolve_device_indices, settings)
                return tarjetas
            except AudioDeviceError as e:
                if not avisado:
                    logger.warning(
                        f"Hay tarjeta de sonido ({', '.join(sorted(tarjetas))}) pero los "
                        f"dispositivos configurados no resuelven; se sigue esperando. {e}"
                    )
                    avisado = True
        elif not avisado:
            logger.warning(
                "Sin tarjeta de sonido: el audio de la sala queda a la espera. "
                "Conéctala cuando quieras; la telefonía sigue funcionando."
            )
            avisado = True
        await asyncio.sleep(intervalo_secs)


@contextmanager
def portaudio() -> Iterator[pyaudio.PyAudio]:
    """Abre PortAudio y garantiza su cierre.

    PortAudio deja hilos vivos si no se llama a `terminate()`, lo que hace que
    el proceso no acabe de salir nunca.
    """
    silence_alsa_warnings()
    # La construcción de PyAudio es donde PortAudio sondea todos sus back-ends
    # (ALSA, JACK, OSS) y donde libjack se queja de que no hay servidor.
    with suppressed_stderr():
        pa = pyaudio.PyAudio()
    try:
        yield pa
    finally:
        with suppressed_stderr():
            pa.terminate()


def list_devices(pa: pyaudio.PyAudio) -> list[AudioDevice]:
    """Enumera todos los dispositivos visibles para PortAudio."""
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        devices.append(
            AudioDevice(
                index=i,
                name=str(info["name"]),
                max_input_channels=int(info["maxInputChannels"]),
                max_output_channels=int(info["maxOutputChannels"]),
                default_sample_rate=float(info["defaultSampleRate"]),
            )
        )
    return devices


def find_device(devices: list[AudioDevice], pattern: str, *, for_input: bool) -> AudioDevice:
    """Busca el dispositivo cuyo nombre case con `pattern`.

    La búsqueda es por subcadena y sin distinguir mayúsculas. Se prefiere una
    coincidencia exacta si existe, para que `default` no acabe eligiendo
    `default:CARD=Device` u otro nombre que simplemente lo contenga.

    Args:
        devices: Dispositivos disponibles.
        pattern: Nombre o fragmento de nombre a buscar.
        for_input: True para exigir capacidad de captura, False de reproducción.

    Returns:
        El dispositivo encontrado.

    Raises:
        AudioDeviceError: Si ninguno casa, con la lista completa en el mensaje
            para que el usuario pueda corregir la configuración sin adivinar.
    """
    candidates = [d for d in devices if (d.can_record if for_input else d.can_play)]
    needle = pattern.strip().lower()

    exact = [d for d in candidates if d.name.lower() == needle]
    if exact:
        return exact[0]

    partial = [d for d in candidates if needle in d.name.lower()]
    if partial:
        return partial[0]

    kind = "entrada" if for_input else "salida"
    available = "\n".join(f"    {d.describe()}" for d in candidates)
    raise AudioDeviceError(
        f"No hay ningún dispositivo de {kind} cuyo nombre contenga '{pattern}'.\n"
        f"  Dispositivos de {kind} disponibles:\n{available}\n"
        f"  Ajusta AUDIO_{'INPUT' if for_input else 'OUTPUT'}_DEVICE en el fichero .env."
    )


def resolve_device_indices(settings: Settings) -> tuple[int, int]:
    """Resuelve los índices de PortAudio de entrada y salida configurados.

    Args:
        settings: Configuración del agente.

    Returns:
        Tupla `(índice_entrada, índice_salida)`.
    """
    with portaudio() as pa:
        devices = list_devices(pa)
        entrada = find_device(devices, settings.audio_input_device, for_input=True)
        salida = find_device(devices, settings.audio_output_device, for_input=False)

    logger.info(f"Dispositivo de entrada: {entrada.describe().strip()}")
    logger.info(f"Dispositivo de salida:  {salida.describe().strip()}")
    return entrada.index, salida.index


def _rms_and_peak(samples: array.array[int]) -> tuple[float, int]:
    """Calcula el valor eficaz y el pico absoluto de una señal PCM de 16 bits."""
    if not samples:
        return 0.0, 0
    rms = math.sqrt(sum(float(s) * s for s in samples) / len(samples))
    return rms, max(abs(s) for s in samples)


def _evaluar_vad(audio: bytes, settings: Settings) -> tuple[int, int, float]:
    """Pasa el audio grabado por el mismo VAD que usará el agente.

    Args:
        audio: PCM de 16 bits, mono, a la frecuencia del pipeline.
        settings: Configuración del agente.

    Returns:
        Tupla `(bloques analizados, bloques que disparan, volumen máximo)`.
    """
    import asyncio

    from pipecat.audio.utils import calculate_audio_volume
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams, VADState

    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=settings.effective_vad_confidence,
            start_secs=settings.effective_vad_start_secs,
            stop_secs=settings.effective_vad_stop_secs,
            min_volume=settings.effective_vad_min_volume,
        )
    )
    vad.set_sample_rate(settings.audio_sample_rate)
    ancho = vad.num_frames_required() * 2  # muestras de 16 bits -> bytes

    async def recorrer() -> tuple[int, int, float]:
        bloques = disparos = 0
        vol_max = 0.0
        for i in range(0, len(audio) - ancho, ancho):
            trozo = audio[i : i + ancho]
            bloques += 1
            vol_max = max(vol_max, calculate_audio_volume(trozo, settings.audio_sample_rate))
            if await vad.analyze_audio(trozo) in (VADState.SPEAKING, VADState.STARTING):
                disparos += 1
        return bloques, disparos, vol_max

    return asyncio.run(recorrer())


def diagnose_noise(settings: Settings, fase_secs: float = 10.0) -> int:
    """Aísla qué mantiene vivo un zumbido o pitido en la salida de audio.

    Muchos adaptadores USB baratos —el PCM2902 de esta placa entre ellos—
    emiten un silbido audible mientras su conversor está activo, aunque lo que
    reciban sea silencio digital. El agente mantiene los flujos de audio
    abiertos de forma continua, así que el ruido dura toda la sesión.

    Esta prueba abre los flujos por separado y va anunciando cada fase, para
    poder identificar de oído cuál lo provoca. Es lo único que sirve aquí: el
    ruido está en la salida analógica y no hay forma de capturarlo desde la
    propia placa.

    Args:
        settings: Configuración del agente.
        fase_secs: Duración de cada fase.

    Returns:
        Siempre 0. El veredicto lo pone quien escucha.
    """
    rate = settings.audio_sample_rate
    chunk = rate // 100

    def cuenta(texto: str) -> None:
        print(f"\n>>> {texto}")
        for restante in range(int(fase_secs), 0, -1):
            print(f"\r    escuchando... {restante:2d}s ", end="", flush=True)
            time.sleep(1)
        print("\r" + " " * 30 + "\r", end="")

    print("\nPrueba de ruido en la salida. Ponte los auriculares y anota en qué")
    print(f"fases se oye el pitido. Cada fase dura {fase_secs:.0f} segundos.")
    print("\nIMPORTANTE: el servicio debe estar parado (systemctl --user stop voice-agent)")

    with portaudio() as pa:
        devices = list_devices(pa)
        entrada = find_device(devices, settings.audio_input_device, for_input=True)
        salida = find_device(devices, settings.audio_output_device, for_input=False)

        cuenta("FASE 1 de 4 — ningún flujo abierto (referencia de silencio)")

        stream_in = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            input=True,
            input_device_index=entrada.index,
            frames_per_buffer=chunk,
        )
        cuenta("FASE 2 de 4 — solo el MICRÓFONO abierto")
        stream_in.stop_stream()
        stream_in.close()

        stream_out = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            output=True,
            output_device_index=salida.index,
            frames_per_buffer=chunk,
        )
        cuenta("FASE 3 de 4 — solo el ALTAVOZ abierto (recibiendo silencio)")

        stream_in = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            input=True,
            input_device_index=entrada.index,
            frames_per_buffer=chunk,
        )
        cuenta("FASE 4 de 4 — los DOS abiertos (como cuando corre el agente)")
        stream_in.stop_stream()
        stream_in.close()
        stream_out.stop_stream()
        stream_out.close()

        cuenta("FASE 5 — todo cerrado otra vez (debe volver el silencio)")

    print("\n  Qué significa cada resultado:")
    print("    solo en 3 y 4  -> lo provoca tener la salida abierta; se puede")
    print("                      mitigar cerrándola cuando el agente calla.")
    print("    solo en 2 y 4  -> lo provoca la captura; probablemente diafonía")
    print("                      del propio adaptador USB.")
    print("    en todas       -> es del hardware o de la alimentación, ajeno al")
    print("                      agente: prueba otro puerto USB o un hub con fuente.")
    print("    en ninguna     -> el ruido lo genera algo del pipeline, no los")
    print("                      flujos en sí. Dímelo y seguimos por ahí.\n")
    return 0


def diagnose_noise_levels(settings: Settings, fase_secs: float = 10.0) -> int:
    """Prueba si algún nivel del mezclador atenúa la diafonía del adaptador.

    Se usa cuando `--diagnose-noise` ya ha señalado a la captura como origen del
    zumbido: en los adaptadores USB baratos el conversor de entrada inyecta
    ruido en la salida de auriculares a través de la alimentación compartida.

    Mantiene el micrófono abierto todo el rato —que es la condición que provoca
    el ruido— y va cambiando los niveles del mezclador para ver si alguno lo
    atenúa. La fase con la salida silenciada es la más informativa: si el
    zumbido sigue oyéndose con el volumen a cero, la interferencia entra
    después del control de volumen y no hay nada que ajustar.

    Args:
        settings: Configuración del agente.
        fase_secs: Duración de cada fase.

    Returns:
        0 siempre; el veredicto lo pone quien escucha.
    """
    import subprocess

    rate = settings.audio_sample_rate

    def mezclador(control: str, valor: str) -> None:
        subprocess.run(
            ["amixer", "-c", "Device", "sset", control, valor],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def cuenta(texto: str) -> None:
        print(f"\n>>> {texto}")
        for restante in range(int(fase_secs), 0, -1):
            print(f"\r    escuchando... {restante:2d}s ", end="", flush=True)
            time.sleep(1)
        print("\r" + " " * 30 + "\r", end="")

    print("\nEl micrófono estará abierto TODO el rato: es lo que provoca el zumbido.")
    print("Lo que cambia en cada fase son los niveles del mezclador.")
    print("Anota en cuáles se atenúa o desaparece.")

    with portaudio() as pa:
        devices = list_devices(pa)
        entrada = find_device(devices, settings.audio_input_device, for_input=True)
        stream_in = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            input=True,
            input_device_index=entrada.index,
            frames_per_buffer=rate // 100,
        )
        try:
            mezclador("Speaker", "75%")
            mezclador("Mic", "14")
            cuenta("FASE 1 de 4 — niveles actuales (salida 75 %, micro 14/16)")

            mezclador("Speaker", "25%")
            cuenta("FASE 2 de 4 — salida al 25 % (¿baja el zumbido con la voz?)")

            mezclador("Speaker", "75%")
            mezclador("Mic", "4")
            cuenta("FASE 3 de 4 — salida al 75 %, ganancia de micro a 4/16")

            mezclador("Mic", "14")
            mezclador("Speaker", "0%")
            cuenta("FASE 4 de 4 — salida SILENCIADA por completo")
        finally:
            stream_in.stop_stream()
            stream_in.close()
            mezclador("Speaker", "75%")
            mezclador("Mic", "14")

    print("\n  Qué significa cada resultado:")
    print("    se va en la 4      -> la interferencia entra ANTES del control de")
    print("                          volumen. Bajar la salida ayuda, a costa de")
    print("                          oír también más flojo al agente.")
    print("    sigue en la 4      -> entra DESPUÉS del volumen: no hay ajuste")
    print("                          posible, es el adaptador. Toca cambiarlo o")
    print("                          separar micrófono y auriculares.")
    print("    baja mucho en la 3 -> depende de la ganancia de captura; se puede")
    print("                          buscar un punto intermedio, aunque bajarla")
    print("                          ya dejó al agente sordo una vez.\n")
    print("  Los niveles quedan restaurados a 75 % y 14/16.\n")
    return 0


def check(settings: Settings, seconds: float = 4.0) -> int:
    """Graba y reproduce por los dispositivos configurados, e informa del nivel.

    Reproduce la comprobación exacta que hará el agente: misma frecuencia de
    muestreo, mismo número de canales y mismos dispositivos.

    Args:
        settings: Configuración del agente.
        seconds: Duración de la grabación de prueba.

    Returns:
        Código de salida: 0 si todo fue bien, 1 si el nivel captado es tan bajo
        que el VAD no llegaría a dispararse nunca.
    """
    rate = settings.audio_sample_rate
    chunk = rate // 100  # bloques de 10 ms, como los que usa Pipecat

    with portaudio() as pa:
        devices = list_devices(pa)

        print("\nDispositivos que ve PortAudio:")
        for d in devices:
            print(f"  {d.describe()}")

        entrada = find_device(devices, settings.audio_input_device, for_input=True)
        salida = find_device(devices, settings.audio_output_device, for_input=False)
        print(f"\nEntrada elegida: {entrada.describe().strip()}")
        print(f"Salida elegida:  {salida.describe().strip()}")

        stream_in = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            input=True,
            input_device_index=entrada.index,
            frames_per_buffer=chunk,
        )

        # Cuenta atrás antes de empezar. Sin ella no se sabe en qué momento hay
        # que hablar: el mensaje aparece y la grabación arranca a la vez, así
        # que para cuando uno reacciona la prueba ya va por la mitad.
        print("\nDi una frase cualquiera, por ejemplo: «¿cuántos núcleos tiene esta placa?»")
        for cuenta in (3, 2, 1):
            print(f"   {cuenta}...", end="", flush=True)
            # Se lee del micrófono durante la espera, en vez de dormir, para
            # que el búfer no se acumule y la grabación empiece limpia.
            for _ in range(int(rate / chunk)):
                stream_in.read(chunk, exception_on_overflow=False)
        print("\n")

        raw = bytearray()
        bloques = int(rate / chunk * seconds)
        # Los bloques son de 10 ms; redibujar en cada uno son cientos de
        # actualizaciones por segundo, ilegibles y ruidosas si la salida no es
        # un terminal. Se refresca diez veces por segundo, que es de sobra para
        # que el medidor se vea reaccionar.
        cada = max(1, int(rate / chunk / 10))
        pico_ventana = 0
        for i in range(bloques):
            datos = stream_in.read(chunk, exception_on_overflow=False)
            raw.extend(datos)

            muestras_bloque = array.array("h")
            muestras_bloque.frombytes(datos)
            _, pico_bloque = _rms_and_peak(muestras_bloque)
            pico_ventana = max(pico_ventana, pico_bloque)

            if i % cada == 0:
                # Escala x4 sobre el fondo de escala: al hablar a distancia
                # normal se ocupa una fracción pequeña del rango, y sin
                # amplificar la barra no se movería lo suficiente para dar
                # sensación de respuesta.
                nivel = min(40, int(pico_ventana / 32767 * 40 * 4))
                restante = seconds - i * chunk / rate
                print(
                    f"\r  HABLA  [{'#' * nivel}{'.' * (40 - nivel)}] {restante:3.1f}s ",
                    end="",
                    flush=True,
                )
                pico_ventana = 0
        print("\r" + " " * 62 + "\r", end="")

        stream_in.stop_stream()
        stream_in.close()

        samples = array.array("h")
        samples.frombytes(bytes(raw))
        rms, peak = _rms_and_peak(samples)
        fondo_escala = peak / 327.67  # porcentaje respecto a 32767

        print(
            f"\n  muestras = {len(samples)}  RMS = {rms:.1f}  pico = {peak} ({fondo_escala:.1f}% fondo de escala)"
        )

        # Lo que de verdad decide si el agente te escucha no es el nivel, sino
        # el VAD. La condición de Pipecat es `confianza >= umbral Y volumen >=
        # min_volume`, y subir el segundo para que el agente no se interrumpa a
        # sí mismo puede dejarlo tan alto que tampoco te oiga a ti. Conviene
        # verlo aquí y no descubrirlo hablándole sin respuesta.
        bloques, disparos, vol_max = _evaluar_vad(bytes(raw), settings)
        print(
            f"  VAD ...... {disparos} de {bloques} bloques superarían el umbral "
            f"(confianza {settings.effective_vad_confidence}, "
            f"volumen mínimo {settings.effective_vad_min_volume})"
        )
        print(f"             volumen máximo alcanzado: {vol_max:.2f}")

        print("\nReproduciendo lo grabado...")
        stream_out = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            output=True,
            output_device_index=salida.index,
            frames_per_buffer=chunk,
        )
        stream_out.write(bytes(raw))
        stream_out.stop_stream()
        stream_out.close()

    print()
    if fondo_escala > 95:
        print("  AVISO: la señal satura. Baja la ganancia de captura para evitar recortes,")
        print("  que degradan mucho la transcripción:  amixer -c Device sset 'Mic' 6 cap")
        return 1
    if disparos == 0:
        print("  Si has hablado durante la prueba, el agente NO te habría oído: ningún")
        print("  bloque supera el umbral del VAD. Dos ajustes posibles:")
        print("     subir la ganancia   ->  amixer -c Device sset 'Mic' 12 cap")
        print("     o bajar el umbral   ->  VAD_MIN_VOLUME=0.55 en el fichero .env")
        print("  Guarda la ganancia con:  sudo alsactl store")
        print()
        print("  Si NO has hablado, esto es justo lo que se busca: el ruido de fondo")
        print("  no dispara detecciones falsas.")
        return 1

    print(f"  Camino de audio correcto: el VAD detectaría voz en {disparos} bloques.")
    return 0


def main() -> int:
    """Punto de entrada de la utilidad de audio."""
    parser = argparse.ArgumentParser(
        description="Lista y comprueba los dispositivos de audio usados por el agente."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Además de listar, graba y reproduce para validar el camino completo.",
    )
    parser.add_argument(
        "--seconds", type=float, default=4.0, help="Duración de la grabación de prueba."
    )
    parser.add_argument(
        "--diagnose-noise-levels",
        action="store_true",
        help="Prueba si algún nivel del mezclador atenúa la diafonía del adaptador.",
    )
    parser.add_argument(
        "--diagnose-noise",
        action="store_true",
        help="Aísla por fases qué flujo de audio provoca un zumbido o pitido en la salida.",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    if args.diagnose_noise_levels:
        return diagnose_noise_levels(settings)

    if args.diagnose_noise:
        return diagnose_noise(settings)

    if args.check:
        return check(settings, seconds=args.seconds)

    with portaudio() as pa:
        print("\nDispositivos que ve PortAudio:")
        for d in list_devices(pa):
            print(f"  {d.describe()}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
