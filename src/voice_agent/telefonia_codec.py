"""Los códecs del audio de llamada: el pipeline siempre ve 16 kHz.

HFP negocia por llamada uno de dos códecs, y son mundos distintos: CVSD es PCM
crudo de 8 kHz, y mSBC es SBC comprimido a 16 kHz dentro de tramas H2 sobre
eSCO transparente. Exponer esa diferencia al pipeline obligaría a precargar
dos juegos de servicios — Deepgram transcribe el flujo crudo y la frecuencia
va fijada en la conexión. En su lugar, cada códec traduce aquí a un contrato
único: **PCM de 16 kHz mono, gane quien gane la negociación**. CVSD se
remuestrea al doble (lineal hacia arriba, diezmado hacia abajo: a calidad
telefónica no hay nada que un filtro fino pudiera rescatar), y mSBC se
descodifica con la `libsbc` de BlueZ por ctypes.

El enmarcado H2 de mSBC no es adorno: cada paquete de 60 bytes lleva 2 de
sincronía (0x01 y un número de secuencia que rota entre cuatro valores), 57 de
trama mSBC y 1 de relleno. El deframer busca la sincronía en un flujo de
bytes en vez de confiar en los límites de los paquetes, porque el controlador
puede trocear a su antojo — es lo mismo que hace PulseAudio.

El silencio también es del códec: para CVSD son ceros, pero un cero no es una
trama mSBC válida — el relleno de la cola vacía se sirve como tramas de
silencio **codificadas de verdad**, no como basura que el móvil intentaría
descodificar.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Protocol

from loguru import logger

#: Códecs de `src/hfp.h` de oFono.
CODEC_CVSD = 0x01
CODEC_MSBC = 0x02

#: El contrato con el pipeline: siempre esta frecuencia, mono, 16 bits.
FRECUENCIA_PIPELINE = 16000

#: Trama mSBC: 2 B de cabecera H2 + 57 B de SBC + 1 B de relleno.
TRAMA_H2 = 60
TRAMA_MSBC = 57
#: Una trama mSBC descodifica a 120 muestras de 16 kHz: 240 B de PCM.
PCM_POR_TRAMA = 240

#: Los cuatro números de secuencia de la cabecera H2, en orden de rotación.
SECUENCIA_H2 = (0x08, 0x38, 0xC8, 0xF8)


class Codec(Protocol):
    """Lo que el transporte necesita de un códec, sin saber cuál es."""

    def decodificar(self, datos: bytes) -> bytes:
        """Bytes de línea → PCM de 16 kHz (lo que haya completo)."""
        ...

    def codificar(self, pcm: bytes) -> bytes:
        """PCM de 16 kHz → bytes de línea (lo que haya completo)."""
        ...

    def silencio(self, n: int) -> bytes:
        """`n` bytes de línea que suenen a silencio de verdad."""
        ...


class CodecCVSD:
    """CVSD visto desde el kernel: PCM de 8 kHz que aquí se remuestrea al doble."""

    def decodificar(self, datos: bytes) -> bytes:
        """8 kHz → 16 kHz por interpolación lineal entre muestras."""
        muestras = memoryview(datos).cast("h")
        salida = bytearray(len(datos) * 2)
        vista = memoryview(salida).cast("h")
        n = len(muestras)
        for i in range(n):
            actual = muestras[i]
            siguiente = muestras[i + 1] if i + 1 < n else actual
            vista[2 * i] = actual
            vista[2 * i + 1] = (actual + siguiente) // 2
        return bytes(salida)

    def codificar(self, pcm: bytes) -> bytes:
        """16 kHz → 8 kHz quedándose con una de cada dos muestras."""
        muestras = memoryview(pcm).cast("h")
        salida = bytearray(len(pcm) // 2)
        vista = memoryview(salida).cast("h")
        for i in range(len(vista)):
            vista[i] = muestras[2 * i]
        return bytes(salida)

    def silencio(self, n: int) -> bytes:
        """El silencio de PCM crudo son ceros, sin más misterio."""
        return bytes(n)


class _SBC(ctypes.Structure):
    """`sbc_t` de `sbc/sbc.h`, con hueco de sobra para no quedarse corto."""

    _fields_ = [
        ("flags", ctypes.c_ulong),
        ("frequency", ctypes.c_uint8),
        ("blocks", ctypes.c_uint8),
        ("subbands", ctypes.c_uint8),
        ("mode", ctypes.c_uint8),
        ("allocation", ctypes.c_uint8),
        ("bitpool", ctypes.c_uint8),
        ("endian", ctypes.c_uint8),
        ("priv", ctypes.c_void_p),
        ("priv_alloc_base", ctypes.c_void_p),
        ("_reserva", ctypes.c_uint8 * 32),
    ]


def _cargar_libsbc() -> ctypes.CDLL:
    nombre = ctypes.util.find_library("sbc") or "libsbc.so.1"
    lib = ctypes.CDLL(nombre)
    lib.sbc_init_msbc.argtypes = [ctypes.POINTER(_SBC), ctypes.c_ulong]
    lib.sbc_init_msbc.restype = ctypes.c_int
    lib.sbc_decode.argtypes = [
        ctypes.POINTER(_SBC),
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.sbc_decode.restype = ctypes.c_ssize_t
    lib.sbc_encode.argtypes = [
        ctypes.POINTER(_SBC),
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_ssize_t),
    ]
    lib.sbc_encode.restype = ctypes.c_ssize_t
    lib.sbc_finish.argtypes = [ctypes.POINTER(_SBC)]
    lib.sbc_finish.restype = None
    return lib


class CodecMSBC:
    """mSBC sobre tramas H2, con la `libsbc` de BlueZ debajo."""

    def __init__(self) -> None:
        """Prepara dos contextos SBC: uno para cada sentido."""
        self._lib = _cargar_libsbc()
        self._dec = _SBC()
        self._enc = _SBC()
        if self._lib.sbc_init_msbc(ctypes.byref(self._dec), 0) != 0:
            raise RuntimeError("sbc_init_msbc (descodificador) falló")
        if self._lib.sbc_init_msbc(ctypes.byref(self._enc), 0) != 0:
            raise RuntimeError("sbc_init_msbc (codificador) falló")
        self._entrante = bytearray()
        self._secuencia = 0
        self._silencio_h2: list[bytes] = self._preparar_silencio()

    def __del__(self) -> None:
        """Libera los contextos de la librería C."""
        lib = getattr(self, "_lib", None)
        if lib is not None:
            lib.sbc_finish(ctypes.byref(self._dec))
            lib.sbc_finish(ctypes.byref(self._enc))

    def _preparar_silencio(self) -> list[bytes]:
        """Codifica un ciclo entero de tramas H2 de silencio, una por secuencia."""
        tramas = []
        secuencia_original = self._secuencia
        for _ in SECUENCIA_H2:
            tramas.append(self._envolver(self._codificar_trama(bytes(PCM_POR_TRAMA))))
        self._secuencia = secuencia_original
        return tramas

    def decodificar(self, datos: bytes) -> bytes:
        """Busca tramas H2 completas en el flujo y las descodifica.

        El resto que no forme trama se queda esperando bytes nuevos; los bytes
        que no casan con ninguna sincronía se descartan de uno en uno, que es
        el precio de resincronizar cuando el controlador trocea a su aire.
        """
        self._entrante.extend(datos)
        pcm = bytearray()
        while True:
            inicio = self._buscar_sincronia()
            if inicio is None or len(self._entrante) - inicio < TRAMA_H2:
                break
            trama = bytes(self._entrante[inicio + 2 : inicio + 2 + TRAMA_MSBC])
            del self._entrante[: inicio + TRAMA_H2]
            pcm.extend(self._decodificar_trama(trama))
        return bytes(pcm)

    def _buscar_sincronia(self) -> int | None:
        datos = self._entrante
        for i in range(len(datos) - 1):
            if datos[i] == 0x01 and datos[i + 1] in SECUENCIA_H2:
                if i:
                    logger.debug(f"mSBC: {i} bytes descartados buscando sincronía")
                return i
        return None

    def _decodificar_trama(self, trama: bytes) -> bytes:
        salida = ctypes.create_string_buffer(PCM_POR_TRAMA)
        escrito = ctypes.c_size_t(0)
        leido = self._lib.sbc_decode(
            ctypes.byref(self._dec),
            trama,
            len(trama),
            salida,
            len(salida),
            ctypes.byref(escrito),
        )
        if leido <= 0:
            logger.debug(f"mSBC: trama indescodificable ({leido}); sustituida por silencio")
            return bytes(PCM_POR_TRAMA)
        return salida.raw[: escrito.value]

    def codificar(self, pcm: bytes) -> bytes:
        """PCM de 16 kHz → tramas H2 completas (240 B de PCM por trama).

        El PCM que no complete trama se pierde: quien llama manda bloques
        grandes del TTS y el descarte es de milisegundos al final de cada
        frase, no audio del medio.
        """
        salida = bytearray()
        for i in range(0, len(pcm) - PCM_POR_TRAMA + 1, PCM_POR_TRAMA):
            salida.extend(self._envolver(self._codificar_trama(pcm[i : i + PCM_POR_TRAMA])))
        return bytes(salida)

    def _codificar_trama(self, pcm: bytes) -> bytes:
        salida = ctypes.create_string_buffer(TRAMA_MSBC + 4)
        escrito = ctypes.c_ssize_t(0)
        consumido = self._lib.sbc_encode(
            ctypes.byref(self._enc),
            pcm,
            len(pcm),
            salida,
            len(salida),
            ctypes.byref(escrito),
        )
        if consumido <= 0 or escrito.value <= 0:
            raise RuntimeError(f"sbc_encode falló: {consumido}")
        return salida.raw[: escrito.value]

    def _envolver(self, trama: bytes) -> bytes:
        cabecera = bytes((0x01, SECUENCIA_H2[self._secuencia]))
        self._secuencia = (self._secuencia + 1) % len(SECUENCIA_H2)
        return cabecera + trama + b"\x00" * (TRAMA_H2 - 2 - len(trama))

    def silencio(self, n: int) -> bytes:
        """Tramas H2 de silencio codificado, cortadas al tamaño pedido.

        El corte puede partir una trama; el deframer del otro lado
        resincroniza por la cabecera, igual que hacemos nosotros.
        """
        salida = bytearray()
        i = 0
        while len(salida) < n:
            salida.extend(self._silencio_h2[i % len(self._silencio_h2)])
            i += 1
        return bytes(salida[:n])


def crear_codec(codec: int) -> Codec:
    """El códec que toque según lo negociado, listo para usar."""
    if codec == CODEC_MSBC:
        return CodecMSBC()
    return CodecCVSD()


__all__ = [
    "CODEC_CVSD",
    "CODEC_MSBC",
    "FRECUENCIA_PIPELINE",
    "PCM_POR_TRAMA",
    "TRAMA_H2",
    "TRAMA_MSBC",
    "Codec",
    "CodecCVSD",
    "CodecMSBC",
    "crear_codec",
]
