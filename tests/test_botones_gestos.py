"""El detector de gestos.

Toda la batería usa un reloj simulado: no hay un solo `sleep`, así que se pueden
probar mantenidos de diez segundos en microsegundos de test.

Lo que se fija aquí es lo que el hardware impone y lo que un refactor rompería en
silencio: que `KEY_MUTE` no tenga niveles, que las fronteras estén donde se dice,
que el rebote no genere gestos de más y que dos teclas a la vez no hagan nada.
"""

from __future__ import annotations

from typing import Any

from voice_agent_botones.gestos import (
    KEY_MUTE,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    DetectorDeGestos,
    Gesto,
    Nivel,
    Pulsacion,
)

VENTANA_MS = 120
LARGO_MS = 700
MUY_LARGO_MS = 2500


class Reloj:
    """Reloj monótono manual, en segundos."""

    def __init__(self) -> None:
        self.t = 1000.0

    def avanzar_ms(self, ms: float) -> float:
        self.t += ms / 1000.0
        return self.t

    def __call__(self) -> float:
        return self.t


def _detector(**kwargs: Any) -> DetectorDeGestos:
    opciones = {
        "ventana_ms": VENTANA_MS,
        "umbral_largo_ms": LARGO_MS,
        "umbral_muy_largo_ms": MUY_LARGO_MS,
    }
    opciones.update(kwargs)
    return DetectorDeGestos(**opciones)  # type: ignore[arg-type]


def _pulsar_y_soltar(
    detector: DetectorDeGestos, reloj: Reloj, tecla: int, duracion_ms: float
) -> Gesto | None:
    """Simula una pulsación completa y cierra la ventana de agrupación."""
    detector.alimentar(Pulsacion(tecla=tecla, pulsada=True, momento=reloj()))
    detector.alimentar(Pulsacion(tecla=tecla, pulsada=False, momento=reloj.avanzar_ms(duracion_ms)))
    reloj.avanzar_ms(VENTANA_MS)
    return detector.vencimientos(reloj())


# --- Niveles por duración -----------------------------------------------------


def test_un_toque_del_rocker_es_nivel_corto() -> None:
    reloj = Reloj()
    gesto = _pulsar_y_soltar(_detector(), reloj, KEY_VOLUMEUP, 80)
    assert gesto is not None
    assert gesto.tecla == KEY_VOLUMEUP
    assert gesto.nivel is Nivel.CORTO


def test_las_fronteras_de_nivel_estan_donde_se_dice() -> None:
    # Justo por debajo y justo por encima de cada umbral. Es la clase de límite
    # que un refactor mueve sin darse cuenta.
    casos = [
        (LARGO_MS - 1, Nivel.CORTO),
        (LARGO_MS, Nivel.LARGO),
        (MUY_LARGO_MS - 1, Nivel.LARGO),
        (MUY_LARGO_MS, Nivel.MUY_LARGO),
    ]
    for duracion, esperado in casos:
        gesto = _pulsar_y_soltar(_detector(), Reloj(), KEY_VOLUMEDOWN, duracion)
        assert gesto is not None, duracion
        assert gesto.nivel is esperado, f"{duracion} ms deberia ser {esperado}"


def test_la_duracion_se_reporta_en_milisegundos() -> None:
    gesto = _pulsar_y_soltar(_detector(), Reloj(), KEY_VOLUMEUP, 1500)
    assert gesto is not None
    assert 1499 <= gesto.duracion_ms <= 1501


# --- MUTE no tiene niveles ----------------------------------------------------


def test_mute_es_siempre_corto_por_mucho_que_se_mantenga() -> None:
    """El hardware manda pulsar y soltar pegados: un nivel 2 nunca llegaría.

    Se comprueba con una duración absurda a propósito. Si algún día alguien le
    asigna una acción al nivel 2 de MUTE, este test le dice por qué no funciona
    antes de que lo descubra apretando el botón diez segundos.
    """
    gesto = _pulsar_y_soltar(_detector(), Reloj(), KEY_MUTE, 10_000)
    assert gesto is not None
    assert gesto.nivel is Nivel.CORTO


def test_mute_no_pide_despertares_para_cruzar_fronteras() -> None:
    # Si pidiera despertar, el bucle del demonio giraría en vano en cada clic.
    detector = _detector()
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_MUTE, pulsada=True, momento=reloj()))
    assert detector.proximo_despertar is None


def test_mute_se_describe_como_clic() -> None:
    gesto = _pulsar_y_soltar(_detector(), Reloj(), KEY_MUTE, 5)
    assert gesto is not None
    assert str(gesto) == "MUTE clic"


# --- Avisos al cruzar frontera ------------------------------------------------


def test_avisa_una_sola_vez_por_frontera() -> None:
    avisos: list[tuple[int, Nivel]] = []
    detector = _detector(al_cruzar_nivel=lambda t, n: avisos.append((t, n)))
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj()))

    # Varios vencimientos mientras sigue pulsada, incluidos dos después de cruzar
    # cada frontera: el aviso no puede repetirse.
    for ms in (300, 500, 100, 100, 1000, 500, 500, 500):
        reloj.avanzar_ms(ms)
        detector.vencimientos(reloj())

    assert avisos == [(KEY_VOLUMEUP, Nivel.LARGO), (KEY_VOLUMEUP, Nivel.MUY_LARGO)]


def test_el_proximo_despertar_apunta_a_la_frontera_siguiente() -> None:
    detector = _detector()
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj()))
    assert detector.proximo_despertar == LARGO_MS / 1000.0

    reloj.avanzar_ms(LARGO_MS)
    detector.vencimientos(reloj())
    assert detector.proximo_despertar == (MUY_LARGO_MS - LARGO_MS) / 1000.0

    reloj.avanzar_ms(MUY_LARGO_MS)
    detector.vencimientos(reloj())
    assert detector.proximo_despertar is None


def test_sin_nada_pulsado_no_hay_que_despertar() -> None:
    assert _detector().proximo_despertar is None


# --- La ventana de agrupación -------------------------------------------------


def test_un_rebote_no_genera_dos_gestos() -> None:
    """Un rebote mecánico de 8 ms tiene que verse como una sola pulsación."""
    detector = _detector()
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj()))
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=False, momento=reloj.avanzar_ms(4)))
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj.avanzar_ms(8)))
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=False, momento=reloj.avanzar_ms(60)))

    # Antes de que cierre la ventana no hay gesto.
    assert detector.vencimientos(reloj()) is None
    reloj.avanzar_ms(VENTANA_MS)
    gesto = detector.vencimientos(reloj())
    assert gesto is not None
    assert gesto.nivel is Nivel.CORTO


def test_una_autorrepeticion_se_agrupa_en_un_solo_mantenido() -> None:
    """Treinta pares pulsar/soltar en tres segundos son un mantenido de tres segundos.

    Este device no autorrepite —no declara `EV_REP`— pero si otro lo hiciera, o si
    el firmware mandara el par pegado y luego lo repitiera, la duración tiene que
    salir bien igualmente.
    """
    detector = _detector()
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEDOWN, pulsada=True, momento=reloj()))
    for _ in range(30):
        detector.alimentar(
            Pulsacion(tecla=KEY_VOLUMEDOWN, pulsada=False, momento=reloj.avanzar_ms(50))
        )
        detector.alimentar(
            Pulsacion(tecla=KEY_VOLUMEDOWN, pulsada=True, momento=reloj.avanzar_ms(50))
        )
        assert detector.vencimientos(reloj()) is None

    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEDOWN, pulsada=False, momento=reloj()))
    reloj.avanzar_ms(VENTANA_MS)
    gesto = detector.vencimientos(reloj())
    assert gesto is not None
    assert gesto.nivel is Nivel.MUY_LARGO
    assert gesto.duracion_ms >= 3000


def test_con_ventana_cero_el_gesto_sale_al_soltar() -> None:
    detector = _detector(ventana_ms=0)
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj()))
    gesto = detector.alimentar(
        Pulsacion(tecla=KEY_VOLUMEUP, pulsada=False, momento=reloj.avanzar_ms(90))
    )
    assert gesto is not None
    assert gesto.nivel is Nivel.CORTO


# --- Casos raros --------------------------------------------------------------


def test_dos_teclas_a_la_vez_no_hacen_nada() -> None:
    """Con tres botones juntos los acordes accidentales existen.

    Hacer lo que no era es peor que no hacer nada, así que se anulan las dos.
    """
    cancelaciones: list[int] = []
    detector = _detector(al_cancelar=lambda: cancelaciones.append(1))
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj()))
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEDOWN, pulsada=True, momento=reloj.avanzar_ms(30)))
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=False, momento=reloj.avanzar_ms(200)))
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEDOWN, pulsada=False, momento=reloj.avanzar_ms(50)))
    reloj.avanzar_ms(VENTANA_MS * 2)

    assert detector.vencimientos(reloj()) is None
    assert cancelaciones == [1]
    assert not detector.hay_gesto_en_curso


def test_una_soltada_huerfana_se_ignora() -> None:
    """Pasa al acaparar el device con una tecla ya hundida, o al reabrirlo."""
    detector = _detector()
    reloj = Reloj()
    assert detector.alimentar(Pulsacion(tecla=KEY_MUTE, pulsada=False, momento=reloj())) is None
    reloj.avanzar_ms(VENTANA_MS * 2)
    assert detector.vencimientos(reloj()) is None


def test_olvidar_descarta_el_gesto_a_medias() -> None:
    """Tras una reenumeración del USB, el `inicio` guardado ya no significa nada."""
    detector = _detector()
    reloj = Reloj()
    detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=True, momento=reloj()))
    assert detector.hay_gesto_en_curso

    detector.olvidar()
    assert not detector.hay_gesto_en_curso
    assert detector.proximo_despertar is None

    # Y la soltada que llegue después no puede inventar un mantenido larguísimo.
    reloj.avanzar_ms(30_000)
    assert detector.alimentar(Pulsacion(tecla=KEY_VOLUMEUP, pulsada=False, momento=reloj())) is None
    reloj.avanzar_ms(VENTANA_MS)
    assert detector.vencimientos(reloj()) is None


def test_vencimientos_sin_nada_en_curso_no_falla() -> None:
    assert _detector().vencimientos(1234.0) is None
