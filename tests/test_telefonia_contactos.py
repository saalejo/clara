"""Búsqueda en la agenda, desambiguación y caché.

El caso que gobierna el diseño es el de las dos Anas: cuando hay empate, la
herramienta tiene que preguntar y NO marcar. Llamar a la persona equivocada es
mucho más caro que hacer una pregunta de más.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_agent_core.telefonia import Contacto, NumeroContacto
from voice_agent_telefonia.contactos import (
    Agenda,
    hay_ganador_claro,
    redactar_pregunta,
)


def contacto(nombre: str, numero: str, tipo: str = "movil") -> Contacto:
    return Contacto(nombre=nombre, numeros=[NumeroContacto(tipo=tipo, numero=numero)])


@pytest.fixture
def agenda(tmp_path: Path) -> Agenda:
    a = Agenda(tmp_path / "agenda.json")
    a.contactos = [
        contacto("Ana Pérez", "+573001111111"),
        contacto("Ana Gómez", "+573002222222"),
        contacto("Juan Carlos Rodríguez", "+573003333333"),
        contacto("Bárbara Jiménez", "+573004444444"),
        contacto("Mª Ángeles Ruiz", "+573005555555"),
        contacto("Mamá", "3006666666"),
    ]
    return a


class TestBuscar:
    def test_nombre_completo(self, agenda: Agenda) -> None:
        (c,) = agenda.buscar("Juan Carlos Rodríguez")
        assert c.contacto.nombre == "Juan Carlos Rodríguez"
        assert c.puntuacion == 100

    def test_sin_tildes(self, agenda: Agenda) -> None:
        assert agenda.buscar("barbara jimenez")[0].contacto.nombre == "Bárbara Jiménez"

    def test_solo_apellido(self, agenda: Agenda) -> None:
        assert agenda.buscar("Rodríguez")[0].contacto.nombre == "Juan Carlos Rodríguez"

    def test_nombre_parcial(self, agenda: Agenda) -> None:
        assert agenda.buscar("juan car")[0].contacto.nombre == "Juan Carlos Rodríguez"

    def test_transcripcion_fonetica(self, agenda: Agenda) -> None:
        """Lo que de verdad manda Whisper `tiny` cuando oye 'Bárbara'."""
        assert agenda.buscar("Varvara")[0].contacto.nombre == "Bárbara Jiménez"

    def test_desconocido_no_devuelve_nada(self, agenda: Agenda) -> None:
        assert agenda.buscar("Ptolomeo Fitzgerald") == []

    def test_consulta_vacia_no_revienta(self, agenda: Agenda) -> None:
        assert agenda.buscar("") == []
        assert agenda.buscar("   ") == []

    def test_respeta_el_limite(self, agenda: Agenda) -> None:
        assert len(agenda.buscar("ana", limite=1)) == 1

    def test_el_orden_es_estable(self, agenda: Agenda) -> None:
        """Sin desempate por nombre, la pregunta cambiaría de orden entre
        llamadas y los tests serían inestables."""
        assert [c.contacto.nombre for c in agenda.buscar("ana")] == [
            r.contacto.nombre for r in agenda.buscar("ana")
        ]


class TestDesambiguacion:
    def test_dos_anas_es_ambiguo(self, agenda: Agenda) -> None:
        """El caso que justifica toda la regla: NO se puede marcar."""
        coincidencias = agenda.buscar("Ana")
        assert len(coincidencias) >= 2
        assert not hay_ganador_claro(coincidencias)

    def test_un_apellido_desempata(self, agenda: Agenda) -> None:
        assert hay_ganador_claro(agenda.buscar("Ana Pérez"))

    def test_nombre_unico_gana(self, agenda: Agenda) -> None:
        assert hay_ganador_claro(agenda.buscar("Mamá"))

    def test_sin_coincidencias_no_hay_ganador(self, agenda: Agenda) -> None:
        assert not hay_ganador_claro([])

    def test_la_pregunta_usa_nombres_no_numeros(self, agenda: Agenda) -> None:
        pregunta = redactar_pregunta(agenda.buscar("Ana"))
        assert "Ana Pérez" in pregunta
        assert "Ana Gómez" in pregunta
        # Un número leído en voz alta no distingue a nadie.
        assert "573001111111" not in pregunta

    def test_pregunta_con_un_solo_candidato(self) -> None:
        from voice_agent_core.telefonia import Coincidencia

        c = [Coincidencia(contacto=contacto("Ana Pérez", "+571"), puntuacion=70)]
        assert redactar_pregunta(c) == "¿Te refieres a Ana Pérez?"


class TestPorNumero:
    def test_resuelve_quien_llama(self, agenda: Agenda) -> None:
        c = agenda.por_numero("+573001111111")
        assert c is not None
        assert c.nombre == "Ana Pérez"

    def test_tolera_que_falte_el_prefijo_del_pais(self, agenda: Agenda) -> None:
        """La agenda guarda unos con prefijo y otros sin él; la red lo manda
        casi siempre. Sin esta tolerancia, la mitad de las llamadas no tendrían
        nombre."""
        c = agenda.por_numero("3001111111")
        assert c is not None
        assert c.nombre == "Ana Pérez"

    def test_al_reves_tambien(self, agenda: Agenda) -> None:
        c = agenda.por_numero("+573006666666")
        assert c is not None
        assert c.nombre == "Mamá"

    def test_desconocido(self, agenda: Agenda) -> None:
        assert agenda.por_numero("+573009999999") is None

    def test_vacio(self, agenda: Agenda) -> None:
        assert agenda.por_numero("") is None


class TestCache:
    def test_ida_y_vuelta(self, tmp_path: Path, agenda: Agenda) -> None:
        agenda.reemplazar(agenda.contactos)
        otra = Agenda(tmp_path / "agenda.json")
        assert otra.cargar_cache()
        assert len(otra.contactos) == 6
        assert otra.actualizada is not None

    def test_sin_fichero(self, tmp_path: Path) -> None:
        assert not Agenda(tmp_path / "no-existe.json").cargar_cache()

    def test_json_corrupto_no_tumba_el_puente(self, tmp_path: Path) -> None:
        ruta = tmp_path / "agenda.json"
        ruta.write_text("{esto no es json", encoding="utf-8")
        a = Agenda(ruta)
        assert not a.cargar_cache()
        assert a.contactos == []

    def test_agenda_sin_descargar_esta_caducada(self, tmp_path: Path) -> None:
        assert Agenda(tmp_path / "agenda.json").caducada(horas=12)

    def test_recien_descargada_no_esta_caducada(self, agenda: Agenda) -> None:
        agenda.reemplazar(agenda.contactos)
        assert not agenda.caducada(horas=12)


class TestNumeroPreferido:
    def test_prefiere_el_movil(self) -> None:
        c = Contacto(
            nombre="Ana",
            numeros=[
                NumeroContacto(tipo="casa", numero="6011111111"),
                NumeroContacto(tipo="movil", numero="3001111111"),
            ],
        )
        assert c.numero_preferido == "3001111111"

    def test_sin_movil_vale_el_primero(self) -> None:
        c = Contacto(nombre="Ana", numeros=[NumeroContacto(tipo="casa", numero="6011111111")])
        assert c.numero_preferido == "6011111111"

    def test_sin_numeros(self) -> None:
        assert Contacto(nombre="Ana").numero_preferido == ""
