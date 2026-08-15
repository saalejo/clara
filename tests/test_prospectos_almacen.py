"""El almacén de prospectos: la memoria comercial entre conversaciones.

Lo delicado no es el SQL, sino los contratos: la identidad no se borra con
campos vacíos, la adopción muda la conversación a la ficha vieja, y nada de
aquí puede lanzar hacia una conversación en curso — una base corrupta degrada
a "sin memoria".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from voice_agent_core.prospectos import AlmacenProspectos

ID = "a3f9c2d1e8b7460fa1b2c3d4e5f60718"


def almacen_en(tmp_path: Path) -> AlmacenProspectos:
    return AlmacenProspectos(tmp_path / "prospectos.sqlite3")


def registrar(
    almacen: AlmacenProspectos,
    id_conversacion: str = "conv-1",
    id_prospecto: str = ID,
    momento: datetime | None = None,
) -> None:
    almacen.registrar_conversacion(id_conversacion, id_prospecto, momento=momento)


class TestElRegistro:
    def test_una_conversacion_abre_ficha(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen, momento=datetime(2026, 8, 14, 10, 0))

        ficha = almacen.ficha(ID)
        assert ficha is not None
        assert ficha.total_conversaciones == 1
        assert ficha.ultima.momento == "2026-08-14T10:00:00"
        assert ficha.ultima.canal == "web"
        assert ficha.ultimo_brief is None

    def test_quien_nunca_converso_no_tiene_ficha(self, tmp_path: Path) -> None:
        assert almacen_en(tmp_path).ficha("desconocido") is None

    def test_un_id_vacio_no_registra_nada(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen, id_prospecto="  ")
        assert almacen.prospectos() == []

    def test_la_identidad_no_se_borra_si_luego_llega_vacia(self, tmp_path: Path) -> None:
        # En la segunda visita puede dar solo el nombre y callarse la empresa.
        almacen = almacen_en(tmp_path)
        registrar(almacen)
        almacen.identificar(ID, nombre="Marta Ruiz", empresa="Óptica Andina")
        almacen.identificar(ID, nombre="Marta Ruiz", empresa="")

        ficha = almacen.ficha(ID)
        assert ficha is not None
        assert ficha.prospecto.empresa == "Óptica Andina"

    def test_el_brief_va_y_vuelve_entero(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen)
        assert almacen.guardar_brief(
            "conv-1",
            ID,
            empresa_y_contacto="Óptica Andina, Marta Ruiz",
            necesidad="Atender llamadas de citas fuera de horario",
            caso_de_uso="Agendamiento de citas por voz",
            canales="teléfono",
            proximos_pasos="El equipo la contacta esta semana",
        )

        brief = almacen.brief("conv-1")
        assert brief is not None
        assert brief.necesidad == "Atender llamadas de citas fuera de horario"
        ficha = almacen.ficha(ID)
        assert ficha is not None and ficha.ultimo_brief is not None
        assert ficha.ultimo_brief.caso_de_uso == "Agendamiento de citas por voz"

    def test_un_segundo_brief_reescribe_al_primero(self, tmp_path: Path) -> None:
        # Si el modelo afinó un dato al final, la versión buena es la última.
        almacen = almacen_en(tmp_path)
        registrar(almacen)
        almacen.guardar_brief("conv-1", ID, necesidad="borrador")
        almacen.guardar_brief("conv-1", ID, necesidad="versión afinada")

        brief = almacen.brief("conv-1")
        assert brief is not None and brief.necesidad == "versión afinada"

    def test_la_transcripcion_y_el_resumen_caen_en_su_conversacion(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen, momento=datetime(2026, 8, 14, 10, 0))
        registrar(almacen, "conv-2", momento=datetime(2026, 8, 14, 11, 0))

        almacen.anotar_transcripcion("conv-2", "visitante: Hola\nclara: Buenos días")
        almacen.anotar_resumen("conv-2", "Preguntó por precios y colgó")

        ficha = almacen.ficha(ID)
        assert ficha is not None
        assert ficha.ultima.id_conversacion == "conv-2"
        assert "Buenos días" in ficha.ultima.transcripcion
        primera = almacen.conversaciones(ID)[-1]
        assert primera.transcripcion == ""  # la anotación no se desparramó

    def test_anotar_sin_conversacion_no_hace_nada_ni_lanza(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        almacen.anotar_transcripcion("conv-fantasma", "hola")
        assert almacen.conversacion("conv-fantasma") is None

    def test_una_base_corrupta_degrada_a_sin_memoria(self, tmp_path: Path) -> None:
        ruta = tmp_path / "prospectos.sqlite3"
        ruta.write_bytes(b"esto no es una base sqlite, pero pesa mas de cien bytes" * 3)
        almacen = AlmacenProspectos(ruta)

        registrar(almacen)  # no lanza
        assert almacen.ficha(ID) is None
        assert almacen.prospectos() == []
        assert not almacen.guardar_brief("conv-1", ID, necesidad="x")


class TestLaIdentidad:
    def test_encuentra_por_empresa_y_nombre(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen)
        almacen.identificar(ID, nombre="Marta Ruiz", empresa="Óptica Andina")

        encontrado = almacen.buscar_por_identidad("  marta ruiz ", "ÓPTICA ANDINA")
        assert encontrado is not None and encontrado.id == ID

    def test_la_empresa_manda_si_el_nombre_se_oyo_distinto(self, tmp_path: Path) -> None:
        # El STT puede convertir "Marta" en "Martha"; la empresa sola basta.
        almacen = almacen_en(tmp_path)
        registrar(almacen)
        almacen.identificar(ID, nombre="Marta Ruiz", empresa="Óptica Andina")

        encontrado = almacen.buscar_por_identidad("Martha Ruíz", "óptica andina")
        assert encontrado is not None and encontrado.id == ID

    def test_sin_nada_que_buscar_devuelve_none(self, tmp_path: Path) -> None:
        assert almacen_en(tmp_path).buscar_por_identidad("", "") is None

    def test_la_adopcion_muda_la_conversacion_a_la_ficha_vieja(self, tmp_path: Path) -> None:
        # Navegador nuevo (id nuevo), pero se presenta como alguien con ficha.
        almacen = almacen_en(tmp_path)
        registrar(almacen, "conv-vieja", momento=datetime(2026, 8, 10, 10, 0))
        almacen.identificar(ID, nombre="Marta Ruiz", empresa="Óptica Andina")

        registrar(almacen, "conv-nueva", "id-navegador-nuevo", momento=datetime(2026, 8, 14, 10, 0))
        almacen.guardar_brief("conv-nueva", "id-navegador-nuevo", necesidad="ampliar el agente")
        almacen.reasignar_conversacion("conv-nueva", ID)

        ficha = almacen.ficha(ID)
        assert ficha is not None
        assert ficha.total_conversaciones == 2
        assert ficha.ultima.id_conversacion == "conv-nueva"
        assert ficha.ultimo_brief is not None
        assert ficha.ultimo_brief.necesidad == "ampliar el agente"


class TestElPadron:
    def test_varios_prospectos_en_una_pasada(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen, "c1", momento=datetime(2026, 8, 13, 10, 0))
        registrar(almacen, "c2", momento=datetime(2026, 8, 13, 11, 0))
        registrar(almacen, "c3", "otro-id", momento=datetime(2026, 8, 14, 12, 0))

        fichas = almacen.prospectos()

        assert [(f.prospecto.id, f.total_conversaciones) for f in fichas] == [
            ("otro-id", 1),
            (ID, 2),
        ], "ordenadas por la última vez que se vieron, la más reciente primero"

    def test_conserva_identidad_y_ultimo_brief(self, tmp_path: Path) -> None:
        almacen = almacen_en(tmp_path)
        registrar(almacen, "c1", momento=datetime(2026, 8, 13, 10, 0))
        almacen.identificar(ID, nombre="Marta", empresa="Óptica Andina")
        almacen.guardar_brief("c1", ID, necesidad="citas por voz")

        (ficha,) = almacen.prospectos()

        assert ficha.prospecto.empresa == "Óptica Andina"
        assert ficha.ultimo_brief is not None
        assert ficha.ultimo_brief.necesidad == "citas por voz"

    def test_un_prospecto_sin_conversaciones_no_produce_ficha(self, tmp_path: Path) -> None:
        """Es la regla que ya tiene `ficha()`, y el JOIN la conserva."""
        almacen = almacen_en(tmp_path)
        almacen.identificar(ID, nombre="Marta")

        assert almacen.prospectos() == []

    def test_una_base_ilegible_degrada_a_vacio(self, tmp_path: Path) -> None:
        ruta = tmp_path / "prospectos.sqlite3"
        ruta.write_text("esto no es una base de datos", encoding="utf-8")

        assert AlmacenProspectos(ruta).prospectos() == []


class TestLaMigracionDeColumnas:
    def test_una_base_del_esquema_anterior_se_migra_sola(self, tmp_path: Path) -> None:
        """La base vivirá en la placa con conversaciones dentro.

        `_ESQUEMA` es todo `CREATE TABLE IF NOT EXISTS`, así que sobre una base
        ya creada no ejecuta nada: toda columna futura tiene que declararse en
        `_COLUMNAS_AÑADIDAS` o cada anotación fallará con `no such column` —
        tragado por el `except` de la casa, es decir, en silencio. Este test
        simula esa situación quitándole una columna a una base vieja y
        comprobando que el `ALTER` la repone.
        """
        from voice_agent_core import prospectos as modulo

        ruta = tmp_path / "prospectos.sqlite3"
        with sqlite3.connect(ruta) as vieja:
            vieja.executescript(_ESQUEMA_SIN_RESUMEN)
            vieja.execute(
                "INSERT INTO conversaciones (id_conversacion, id_prospecto, momento) "
                "VALUES ('vieja-1', ?, '2026-08-01T10:00:00')",
                (ID,),
            )
            vieja.execute(
                "INSERT INTO prospectos (id, creado_en, actualizado_en) "
                "VALUES (?, '2026-08-01T10:00:00', '2026-08-01T10:00:00')",
                (ID,),
            )

        original = modulo._COLUMNAS_AÑADIDAS["conversaciones"]
        modulo._COLUMNAS_AÑADIDAS["conversaciones"] = (
            *original,
            ("resumen", "TEXT NOT NULL DEFAULT ''"),
        )
        try:
            almacen = AlmacenProspectos(ruta)
            almacen.anotar_resumen("vieja-1", "retomado tras la migración")
            ficha = almacen.ficha(ID)
        finally:
            modulo._COLUMNAS_AÑADIDAS["conversaciones"] = original

        assert ficha is not None, "la conversación de antes de la migración se perdió"
        assert ficha.ultima.resumen == "retomado tras la migración"


#: La tabla `conversaciones` como habría nacido sin la columna `resumen`, más
#: las otras dos al día: solo se ensaya la mecánica del ALTER por tabla.
_ESQUEMA_SIN_RESUMEN = """
CREATE TABLE conversaciones (
    id_conversacion TEXT PRIMARY KEY,
    id_prospecto TEXT NOT NULL,
    momento TEXT NOT NULL,
    canal TEXT NOT NULL DEFAULT 'web',
    transcripcion TEXT NOT NULL DEFAULT ''
);
CREATE TABLE prospectos (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL DEFAULT '',
    empresa TEXT NOT NULL DEFAULT '',
    contacto TEXT NOT NULL DEFAULT '',
    creado_en TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE TABLE briefs (
    id_conversacion TEXT PRIMARY KEY,
    id_prospecto TEXT NOT NULL,
    momento TEXT NOT NULL,
    empresa_y_contacto TEXT NOT NULL DEFAULT '',
    necesidad TEXT NOT NULL DEFAULT '',
    caso_de_uso TEXT NOT NULL DEFAULT '',
    canales TEXT NOT NULL DEFAULT '',
    integraciones TEXT NOT NULL DEFAULT '',
    plazo_y_presupuesto TEXT NOT NULL DEFAULT '',
    proximos_pasos TEXT NOT NULL DEFAULT '',
    notas TEXT NOT NULL DEFAULT ''
);
"""
