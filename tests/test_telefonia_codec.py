"""Los códecs de llamada: remuestreo CVSD, enmarcado H2 y mSBC de verdad.

Los tests de mSBC ejercitan la `libsbc` real — está instalada en la placa,
que es donde corre la batería. Lo que se fija no es la fidelidad psicoacústica
sino el contrato: tramas H2 de 60 bytes bien envueltas, resincronización ante
troceos arbitrarios, y que el silencio codificado descodifique a algo cercano
al silencio, porque un cero crudo no es una trama válida.
"""

from __future__ import annotations

import struct

from voice_agent.telefonia_codec import (
    CODEC_CVSD,
    CODEC_MSBC,
    PCM_POR_TRAMA,
    SECUENCIA_H2,
    TRAMA_H2,
    CodecCVSD,
    CodecMSBC,
    crear_codec,
)


def _tono(muestras: int, paso: int = 800) -> bytes:
    """Una onda simple de 16 bits, suficiente para reconocerse tras el viaje."""
    return b"".join(struct.pack("<h", ((i % paso) - paso // 2) * 30) for i in range(muestras))


class TestCVSD:
    def test_decodificar_duplica_las_muestras(self) -> None:
        pcm8k = struct.pack("<4h", 100, 200, -100, 0)
        pcm16k = CodecCVSD().decodificar(pcm8k)
        assert len(pcm16k) == len(pcm8k) * 2
        # La primera muestra se conserva y la interpolada queda en medio.
        assert struct.unpack("<2h", pcm16k[:4]) == (100, 150)

    def test_codificar_diezma_a_la_mitad(self) -> None:
        pcm16k = struct.pack("<4h", 100, 999, 200, 999)
        pcm8k = CodecCVSD().codificar(pcm16k)
        assert struct.unpack("<2h", pcm8k) == (100, 200)

    def test_ida_y_vuelta_conserva_las_muestras_pares(self) -> None:
        pcm8k = _tono(160)
        codec = CodecCVSD()
        assert codec.codificar(codec.decodificar(pcm8k)) == pcm8k

    def test_el_silencio_son_ceros(self) -> None:
        assert CodecCVSD().silencio(48) == bytes(48)


class TestMSBC:
    def test_codificar_envuelve_en_tramas_h2(self) -> None:
        codec = CodecMSBC()
        linea = codec.codificar(_tono(240))  # 2 tramas justas
        assert len(linea) == 2 * TRAMA_H2
        assert linea[0] == 0x01 and linea[1] == SECUENCIA_H2[0]
        assert linea[TRAMA_H2] == 0x01 and linea[TRAMA_H2 + 1] == SECUENCIA_H2[1]

    def test_ida_y_vuelta_devuelve_el_mismo_numero_de_muestras(self) -> None:
        emisor, receptor = CodecMSBC(), CodecMSBC()
        pcm = _tono(240)
        assert len(receptor.decodificar(emisor.codificar(pcm))) == len(pcm)

    def test_resincroniza_con_el_flujo_troceado(self) -> None:
        """El controlador trocea a su aire: basura delante y cortes arbitrarios."""
        emisor, receptor = CodecMSBC(), CodecMSBC()
        linea = b"\x55\x55" + emisor.codificar(_tono(480))  # 4 tramas y basura delante
        pcm = bytearray()
        for i in range(0, len(linea), 17):  # trozos que no respetan nada
            pcm.extend(receptor.decodificar(linea[i : i + 17]))
        assert len(pcm) == 4 * PCM_POR_TRAMA

    def test_el_silencio_es_valido_y_descodifica_a_silencio(self) -> None:
        codec, receptor = CodecMSBC(), CodecMSBC()
        pcm = receptor.decodificar(codec.silencio(4 * TRAMA_H2))
        assert len(pcm) == 4 * PCM_POR_TRAMA
        muestras = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        assert max(abs(m) for m in muestras) < 50  # silencio, con margen de códec

    def test_el_pcm_incompleto_espera_sin_perderse(self) -> None:
        """Media trama no produce nada; la otra media la completa."""
        emisor, receptor = CodecMSBC(), CodecMSBC()
        linea = emisor.codificar(_tono(120) + _tono(120))
        assert receptor.decodificar(linea[:30]) == b""
        assert len(receptor.decodificar(linea[30:])) == 2 * PCM_POR_TRAMA


class TestFabrica:
    def test_elige_por_el_byte_negociado(self) -> None:
        assert isinstance(crear_codec(CODEC_CVSD), CodecCVSD)
        assert isinstance(crear_codec(CODEC_MSBC), CodecMSBC)
        assert isinstance(crear_codec(99), CodecCVSD)  # lo desconocido, a lo seguro
