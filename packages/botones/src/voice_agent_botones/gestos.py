"""Detector de gestos: de pulsaciones crudas a intenciones.

Máquina de estados **pura**: no hace entrada/salida, no importa asyncio y no
llama a `time`. Recibe el instante en cada `Pulsacion` y, para lo que vence por
sí solo, publica `proximo_despertar` para que quien la usa la mueva. Así los
tests simulan horas de pulsaciones sin un solo `sleep`.

## Los dos hechos del hardware que definen esto

**`KEY_MUTE` no distingue mantener pulsado.** Manda el par pulsar/soltar en el
mismo microsegundo, aunque lo aprietes tres segundos. Medido en la placa:

    27.391s  MUTE   PULSA
    27.391s  MUTE   suelta     <- pegados, tres veces de tres

**El rocker de volumen sí lo distingue**, con duración fiable (2272 ms medidos) y
sin autorrepetición, porque el device no declara `EV_REP`.

De ahí sale el reparto: el rocker tiene tres niveles por duración y MUTE tiene
uno solo. Se modela con `TECLAS_CON_NIVELES`, y no con un `if` sobre el código de
tecla, para que quede en un sitio y con un test detrás.

## La ventana de agrupación hace tres trabajos

Una soltada no es definitiva hasta que pasan `ventana_ms` sin otra pulsación de
la misma tecla. Eso resuelve de una vez:

- el **antirrebote**, mecánico o del firmware HID;
- la **autorrepetición**, si algún device la mandara: treinta pares en tres
  segundos se agrupan en un único mantenido de tres segundos;
- y el caso en que el device manda pulsar/soltar pegados y luego lo repite
  mientras lo mantienes, con lo que la duración sale bien igualmente.

Es también lo que hace que un clic de MUTE cueste solo 120 ms de latencia, frente
a los 300-400 ms que costaría detectar un doble clic. Por eso el mapa no usa
doble clic en ninguna tecla.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

# Códigos de `linux/input-event-codes.h` para las tres teclas que la tarjeta
# emite de verdad. Se definen aquí y no se importan de `evdev` porque son tres
# números estables desde hace veinte años y la alternativa era una extensión en C
# en el uv.lock del workspace.
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115

TECLAS = frozenset({KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP})

# Las que admiten niveles por duración. MUTE queda fuera por el hardware, no por
# gusto: ver el docstring del módulo.
TECLAS_CON_NIVELES = frozenset({KEY_VOLUMEDOWN, KEY_VOLUMEUP})

NOMBRES = {KEY_MUTE: "MUTE", KEY_VOLUMEDOWN: "VOL-", KEY_VOLUMEUP: "VOL+"}


class Nivel(IntEnum):
    """Cuánto se ha mantenido pulsada una tecla.

    Attributes:
        CORTO: Un toque. El único nivel posible en `KEY_MUTE`.
        LARGO: Mantenida más de `umbral_largo_ms`.
        MUY_LARGO: Mantenida más de `umbral_muy_largo_ms`.
    """

    CORTO = 1
    LARGO = 2
    MUY_LARGO = 3


@dataclass(frozen=True)
class Pulsacion:
    """Un evento de tecla ya filtrado, tal como sale del device.

    Attributes:
        tecla: Código de `linux/input-event-codes.h`.
        pulsada: `True` al pulsar, `False` al soltar.
        momento: Instante monótono en segundos, puesto por quien lee el device.
    """

    tecla: int
    pulsada: bool
    momento: float


@dataclass(frozen=True)
class Gesto:
    """Una intención completa, lista para traducirse a una acción.

    Attributes:
        tecla: Código de la tecla.
        nivel: Nivel alcanzado antes de soltar.
        duracion_ms: Cuánto se mantuvo, en milisegundos. Es 0 en las teclas sin
            mantenido, y sirve para el registro, no para decidir.
    """

    tecla: int
    nivel: Nivel
    duracion_ms: int

    def __str__(self) -> str:
        """Describe el gesto como se ve en el log y en `make botones-sonda`."""
        nombre = NOMBRES.get(self.tecla, str(self.tecla))
        if self.tecla not in TECLAS_CON_NIVELES:
            return f"{nombre} clic"
        return f"{nombre} nivel {int(self.nivel)} ({self.duracion_ms} ms)"


@dataclass
class _EnCurso:
    """Estado de una tecla que se está pulsando o acaba de soltarse."""

    tecla: int
    inicio: float
    nivel_avisado: Nivel = Nivel.CORTO
    soltada_en: float | None = None


class DetectorDeGestos:
    """Convierte pulsaciones crudas en gestos.

    No usa el reloj del sistema: el tiempo llega en cada `Pulsacion` y, para lo
    que vence solo, quien la usa consulta `proximo_despertar` y llama a
    `vencimientos`.
    """

    def __init__(
        self,
        *,
        ventana_ms: int = 120,
        umbral_largo_ms: int = 700,
        umbral_muy_largo_ms: int = 2500,
        al_cruzar_nivel: Callable[[int, Nivel], None] | None = None,
        al_cancelar: Callable[[], None] | None = None,
    ) -> None:
        """Configura los umbrales y los avisos.

        Args:
            ventana_ms: Milisegundos sin nueva pulsación para dar una soltada por
                definitiva. Hace de antirrebote.
            umbral_largo_ms: Frontera del nivel 2.
            umbral_muy_largo_ms: Frontera del nivel 3.
            al_cruzar_nivel: Se llama, con la tecla y el nivel nuevo, cuando una
                tecla mantenida cruza una frontera. Sirve para pitar mientras el
                usuario sigue apretando, que es lo que enseña la interfaz sola.
            al_cancelar: Se llama cuando un gesto se descarta por haber pulsado
                dos teclas a la vez.
        """
        self._ventana = ventana_ms / 1000.0
        self._largo = umbral_largo_ms / 1000.0
        self._muy_largo = umbral_muy_largo_ms / 1000.0
        self._al_cruzar_nivel = al_cruzar_nivel
        self._al_cancelar = al_cancelar
        self._actual: _EnCurso | None = None

    # -- Entrada --------------------------------------------------------------

    def alimentar(self, p: Pulsacion) -> Gesto | None:
        """Procesa una pulsación cruda.

        Devuelve un gesto solo en el caso —poco habitual— de que la pulsación
        cierre la ventana de agrupación de otra tecla. Lo normal es que devuelva
        `None` y el gesto salga después por `vencimientos`.
        """
        gesto: Gesto | None = None

        if p.pulsada:
            if self._actual is not None and self._actual.tecla != p.tecla:
                # Dos teclas a la vez. Con tres botones juntos los acordes
                # accidentales existen, y ejecutar lo que no era es peor que no
                # hacer nada: se anulan las dos.
                self._actual = None
                if self._al_cancelar is not None:
                    self._al_cancelar()
                return None
            if self._actual is not None and self._actual.soltada_en is not None:
                # Repetición dentro de la ventana: se reanuda el mantenido en
                # curso en lugar de empezar uno nuevo. Es lo que absorbe el
                # rebote y una eventual autorrepetición.
                self._actual.soltada_en = None
                return None
            if self._actual is None:
                self._actual = _EnCurso(tecla=p.tecla, inicio=p.momento)
            return None

        # Soltada.
        if self._actual is None or self._actual.tecla != p.tecla:
            # Soltada huérfana. Pasa al acaparar el device con una tecla ya
            # hundida, o al reabrirlo tras una reenumeración del USB.
            return None
        self._actual.soltada_en = p.momento
        if self._ventana <= 0:
            gesto = self._cerrar(p.momento)
        return gesto

    def vencimientos(self, ahora: float) -> Gesto | None:
        """Atiende lo que vence por el paso del tiempo.

        Hay dos cosas que vencen sin que llegue ningún evento: el cierre de la
        ventana de agrupación tras una soltada, y el cruce de una frontera de
        nivel con la tecla todavía pulsada. Devuelve el gesto si lo hubo.
        """
        actual = self._actual
        if actual is None:
            return None

        if actual.soltada_en is not None:
            if ahora - actual.soltada_en >= self._ventana:
                return self._cerrar(actual.soltada_en)
            return None

        # Sigue pulsada: avisar de las fronteras que haya cruzado.
        nivel = self._nivel_de(actual.tecla, ahora - actual.inicio)
        while actual.nivel_avisado < nivel:
            actual.nivel_avisado = Nivel(int(actual.nivel_avisado) + 1)
            if self._al_cruzar_nivel is not None:
                self._al_cruzar_nivel(actual.tecla, actual.nivel_avisado)
        return None

    def olvidar(self) -> None:
        """Descarta el gesto en curso.

        Se llama al reabrir el device: si desapareció con una tecla pulsada, el
        `inicio` que había guardado ya no significa nada.
        """
        self._actual = None

    # -- Consulta -------------------------------------------------------------

    @property
    def proximo_despertar(self) -> float | None:
        """Segundos que faltan para el siguiente vencimiento, o `None` si no hay.

        Quien usa el detector lo pasa como timeout de su espera. Devolver `None`
        significa "no hay nada pendiente, puedes esperar indefinidamente".
        """
        actual = self._actual
        if actual is None:
            return None
        if actual.soltada_en is not None:
            return self._ventana
        if actual.tecla not in TECLAS_CON_NIVELES:
            return None
        # Falta avisar de alguna frontera: despertar en la siguiente.
        if actual.nivel_avisado is Nivel.CORTO:
            return self._largo
        if actual.nivel_avisado is Nivel.LARGO:
            return self._muy_largo - self._largo
        return None

    @property
    def hay_gesto_en_curso(self) -> bool:
        """Indica si alguna tecla está pulsada o pendiente de cerrar."""
        return self._actual is not None

    # -- Interno --------------------------------------------------------------

    def _cerrar(self, soltada_en: float) -> Gesto:
        """Cierra el gesto en curso y lo devuelve."""
        actual = self._actual
        assert actual is not None
        self._actual = None
        duracion = soltada_en - actual.inicio
        return Gesto(
            tecla=actual.tecla,
            nivel=self._nivel_de(actual.tecla, duracion),
            # Redondear y no truncar: la duración es un valor de registro, y con
            # `int()` una pulsación de tres segundos justos se informa como 2999 ms
            # por el error de acumulación del reloj, que es feo en un log.
            duracion_ms=round(duracion * 1000),
        )

    def _nivel_de(self, tecla: int, duracion: float) -> Nivel:
        """Traduce una duración a nivel, respetando las teclas sin mantenido."""
        if tecla not in TECLAS_CON_NIVELES:
            return Nivel.CORTO
        if duracion >= self._muy_largo:
            return Nivel.MUY_LARGO
        if duracion >= self._largo:
            return Nivel.LARGO
        return Nivel.CORTO


__all__ = [
    "KEY_MUTE",
    "KEY_VOLUMEDOWN",
    "KEY_VOLUMEUP",
    "NOMBRES",
    "TECLAS",
    "TECLAS_CON_NIVELES",
    "DetectorDeGestos",
    "Gesto",
    "Nivel",
    "Pulsacion",
]
