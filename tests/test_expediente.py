"""La fusión de las dos mitades de una llamada, que es lo que el panel enseña.

Los casos que de verdad importan aquí son los asimétricos: una llamada de
navegador solo existe como JSON y una que se cayó pronto solo existe como fila.
Si la fusión dejara de ser externa completa, cualquiera de las dos desaparecería
del panel **en silencio**, que es la forma más cara de romper esto.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from voice_agent_core.expediente import (
    SIN_FICHA,
    TOPE_DEFECTO,
    CriteriosExpedientes,
    leer_expediente,
    leer_traza,
    listar_expedientes,
    opciones_de_filtro,
)
from voice_agent_core.historial import HistorialPacientes
from voice_agent_core.rutas import dir_alertas, dir_resumenes, dir_trazas, ruta_historial


def _historial(data_dir: Path) -> HistorialPacientes:
    return HistorialPacientes(ruta_historial(data_dir))


def _fila(
    data_dir: Path,
    id_llamada: str,
    numero: str = "3001112233",
    direccion: str = "entrante",
    momento: str = "2026-08-09T12:00:00",
    nombre: str = "",
) -> HistorialPacientes:
    historial = _historial(data_dir)
    historial.registrar_llamada(
        id_llamada, numero, direccion, nombre=nombre, momento=datetime.fromisoformat(momento)
    )
    return historial


def _alerta(data_dir: Path, fichero: str, **campos: Any) -> Path:
    carpeta = dir_alertas(data_dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    datos: dict[str, Any] = {
        "id_llamada": "llamada-x",
        "momento": "2026-08-09T12:00:00",
        "nivel": "verde",
        "sintomas": "Molestia leve.",
        "justificacion": "Sin signos de alarma.",
    }
    datos.update(campos)
    ruta = carpeta / fichero
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return ruta


def _resumen(data_dir: Path, fichero: str, **campos: Any) -> Path:
    carpeta = dir_resumenes(data_dir)
    carpeta.mkdir(parents=True, exist_ok=True)
    datos: dict[str, Any] = {
        "id_llamada": "llamada-x",
        "momento": "2026-08-09T12:05:00",
        "paciente_y_procedimiento": "Nora, cataratas",
        "sintomas": "Molestia leve.",
        "decision": "Seguir en casa.",
        "proximos_pasos": "Llamar si empeora.",
    }
    datos.update(campos)
    ruta = carpeta / fichero
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return ruta


def _listar(data_dir: Path, **campos: Any) -> Any:
    return listar_expedientes(data_dir, CriteriosExpedientes(**campos))


class TestLaFusion:
    def test_el_json_y_el_historial_se_cruzan_por_id(self, tmp_path: Path) -> None:
        _fila(tmp_path, "llamada-a", nombre="Nora")
        _alerta(tmp_path, "20260809-120000.json", id_llamada="llamada-a", nivel="rojo")
        _resumen(tmp_path, "20260809-120500.json", id_llamada="llamada-a")

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.id_llamada == "llamada-a"
        assert expediente.numero == "3001112233"
        assert expediente.nombre == "Nora"
        assert expediente.tiene_ficha
        assert len(expediente.alertas) == 1
        assert expediente.resumen is not None

    def test_una_llamada_solo_en_json_aparece(self, tmp_path: Path) -> None:
        """La de navegador: `numero_identificable` no le abre ficha, y existe igual."""
        _alerta(tmp_path, "20260809-120000.json", id_llamada="llamada-navegador")

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.id_llamada == "llamada-navegador"
        assert not expediente.tiene_ficha
        assert expediente.numero == ""
        assert expediente.direccion == SIN_FICHA
        assert expediente.momento == "2026-08-09T12:00:00"

    def test_una_llamada_solo_en_el_historial_aparece(self, tmp_path: Path) -> None:
        """La que se cortó antes de que corriera ninguna herramienta."""
        _fila(tmp_path, "llamada-cortada")

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.id_llamada == "llamada-cortada"
        assert expediente.alertas == []
        assert expediente.resumen is None
        assert expediente.tiene_ficha

    def test_varias_alertas_de_la_misma_llamada_se_agrupan(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="llamada-a", nivel="verde")
        _alerta(
            tmp_path,
            "20260809-121000.json",
            id_llamada="llamada-a",
            nivel="rojo",
            momento="2026-08-09T12:10:00",
        )

        (expediente,) = _listar(tmp_path).expedientes

        assert [a.nivel for a in expediente.alertas] == ["verde", "rojo"]
        assert expediente.nivel == "rojo", "manda la última alerta, no la primera"

    def test_el_resumen_normal_gana_al_de_respaldo(self, tmp_path: Path) -> None:
        _resumen(
            tmp_path,
            "20260809-120500-respaldo.json",
            id_llamada="llamada-a",
            decision="De respaldo.",
            transcripcion=["Clara: hola"],
        )
        _resumen(tmp_path, "20260809-121000.json", id_llamada="llamada-a", decision="Del modelo.")

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.resumen is not None
        assert expediente.resumen.decision == "Del modelo."
        assert not expediente.es_respaldo

    def test_un_resumen_de_respaldo_solo_se_marca_como_tal(self, tmp_path: Path) -> None:
        _resumen(
            tmp_path,
            "20260809-120500-respaldo.json",
            id_llamada="llamada-a",
            transcripcion=["Clara: hola"],
        )

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.es_respaldo

    def test_el_orden_va_de_la_mas_reciente_a_la_mas_antigua(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260801-100000.json", id_llamada="vieja", momento="2026-08-01T10:00:00")
        _alerta(tmp_path, "20260809-100000.json", id_llamada="nueva", momento="2026-08-09T10:00:00")

        resultado = _listar(tmp_path)

        assert [e.id_llamada for e in resultado.expedientes] == ["nueva", "vieja"]

    def test_el_nivel_del_json_rescata_cuando_la_fila_lo_tiene_vacio(self, tmp_path: Path) -> None:
        """La anotación en SQLite va bajo el `except` de la casa y puede perderse."""
        _fila(tmp_path, "llamada-a")  # sin `anotar_alerta`: nivel vacío
        _alerta(tmp_path, "20260809-120000.json", id_llamada="llamada-a", nivel="rojo")

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.fila is not None
        assert expediente.fila.nivel == ""
        assert expediente.nivel == "rojo"

    def test_el_momento_sale_de_la_fila_porque_es_el_del_descuelgue(self, tmp_path: Path) -> None:
        _fila(tmp_path, "llamada-a", momento="2026-08-09T11:00:00")
        _resumen(tmp_path, "20260809-120500.json", id_llamada="llamada-a")

        (expediente,) = _listar(tmp_path).expedientes

        assert expediente.momento == "2026-08-09T11:00:00"


class TestLosFiltros:
    def _dos_llamadas(self, tmp_path: Path) -> None:
        _alerta(
            tmp_path,
            "20260809-120000.json",
            id_llamada="roja",
            nivel="rojo",
            procedimiento="Cataratas",
            cobertura="cubierta",
        )
        _alerta(
            tmp_path,
            "20260809-130000.json",
            id_llamada="verde",
            nivel="verde",
            momento="2026-08-09T13:00:00",
            procedimiento="apendicitis",
            cobertura="no_cubierta",
        )

    def test_por_nivel(self, tmp_path: Path) -> None:
        self._dos_llamadas(tmp_path)
        resultado = _listar(tmp_path, nivel="rojo")
        assert [e.id_llamada for e in resultado.expedientes] == ["roja"]

    def test_por_procedimiento_ignorando_mayusculas(self, tmp_path: Path) -> None:
        self._dos_llamadas(tmp_path)
        resultado = _listar(tmp_path, procedimiento="cataratas")
        assert [e.id_llamada for e in resultado.expedientes] == ["roja"]

    def test_por_cobertura_que_solo_vive_en_el_json(self, tmp_path: Path) -> None:
        self._dos_llamadas(tmp_path)
        resultado = _listar(tmp_path, cobertura="no_cubierta")
        assert [e.id_llamada for e in resultado.expedientes] == ["verde"]

    def test_por_direccion(self, tmp_path: Path) -> None:
        _fila(tmp_path, "entrante-1", numero="3001112233", direccion="entrante")
        _fila(tmp_path, "mision-1", numero="3004445566", direccion="mision")

        resultado = _listar(tmp_path, direccion="mision")

        assert [e.id_llamada for e in resultado.expedientes] == ["mision-1"]

    def test_sin_ficha_devuelve_solo_las_que_no_abrieron_ficha(self, tmp_path: Path) -> None:
        _fila(tmp_path, "con-ficha")
        _alerta(tmp_path, "20260809-120000.json", id_llamada="con-ficha")
        _alerta(tmp_path, "20260809-130000.json", id_llamada="de-navegador")

        resultado = _listar(tmp_path, direccion=SIN_FICHA)

        assert [e.id_llamada for e in resultado.expedientes] == ["de-navegador"]

    def test_por_numero(self, tmp_path: Path) -> None:
        _fila(tmp_path, "de-nora", numero="3001112233")
        _fila(tmp_path, "de-otro", numero="3004445566")

        resultado = _listar(tmp_path, numero="3001112233")

        assert [e.id_llamada for e in resultado.expedientes] == ["de-nora"]

    def test_por_rango_de_fechas(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260801-100000.json", id_llamada="vieja", momento="2026-08-01T10:00:00")
        _alerta(tmp_path, "20260809-100000.json", id_llamada="nueva", momento="2026-08-09T10:00:00")

        resultado = _listar(tmp_path, desde=date(2026, 8, 5), hasta=date(2026, 8, 9))

        assert [e.id_llamada for e in resultado.expedientes] == ["nueva"]

    def test_el_dia_de_hasta_entra_entero(self, tmp_path: Path) -> None:
        _alerta(
            tmp_path, "20260809-235000.json", id_llamada="tardia", momento="2026-08-09T23:50:00"
        )

        resultado = _listar(tmp_path, desde=date(2026, 8, 9), hasta=date(2026, 8, 9))

        assert [e.id_llamada for e in resultado.expedientes] == ["tardia"]

    def test_una_llamada_de_medianoche_no_pierde_su_resumen(self, tmp_path: Path) -> None:
        """El nombre del fichero lleva cuándo se escribió, no cuándo se descolgó."""
        _fila(tmp_path, "llamada-a", momento="2026-08-09T23:58:00")
        _resumen(
            tmp_path,
            "20260810-000200.json",
            id_llamada="llamada-a",
            momento="2026-08-10T00:02:00",
            decision="Ir a urgencias.",
        )

        resultado = _listar(tmp_path, desde=date(2026, 8, 9), hasta=date(2026, 8, 9))

        (expediente,) = resultado.expedientes
        assert expediente.resumen is not None
        assert expediente.resumen.decision == "Ir a urgencias."

    def test_el_rango_no_abre_los_ficheros_de_fuera(self, tmp_path: Path) -> None:
        for dia in ("20260801", "20260802", "20260803"):
            _alerta(
                tmp_path,
                f"{dia}-100000.json",
                id_llamada=f"l-{dia}",
                momento=f"2026-08-{dia[-2:]}T10:00:00",
            )
        _alerta(tmp_path, "20260820-100000.json", id_llamada="sola", momento="2026-08-20T10:00:00")

        resultado = _listar(tmp_path, desde=date(2026, 8, 20), hasta=date(2026, 8, 20))

        assert [e.id_llamada for e in resultado.expedientes] == ["sola"]
        assert resultado.ficheros_examinados == 1, "los de agosto 1-3 no se abrieron siquiera"

    def test_los_ejes_se_acumulan(self, tmp_path: Path) -> None:
        self._dos_llamadas(tmp_path)
        assert _listar(tmp_path, nivel="rojo", cobertura="no_cubierta").expedientes == []
        assert len(_listar(tmp_path, nivel="rojo", cobertura="cubierta").expedientes) == 1


class TestLaLecturaTolerante:
    def test_un_json_corrupto_se_ignora(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="buena")
        (dir_alertas(tmp_path) / "20260809-130000.json").write_text("{a medias", encoding="utf-8")

        resultado = _listar(tmp_path)

        assert [e.id_llamada for e in resultado.expedientes] == ["buena"]

    def test_un_nivel_inventado_no_tumba_la_lista(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="buena")
        _alerta(tmp_path, "20260809-130000.json", id_llamada="rara", nivel="azul")

        resultado = _listar(tmp_path)

        assert [e.id_llamada for e in resultado.expedientes] == ["buena"]

    def test_un_fichero_sin_fecha_en_el_nombre_no_estorba(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="buena")
        (dir_alertas(tmp_path) / "roto.json").write_text("{", encoding="utf-8")

        assert [e.id_llamada for e in _listar(tmp_path).expedientes] == ["buena"]

    def test_sin_carpetas_no_revienta(self, tmp_path: Path) -> None:
        resultado = _listar(tmp_path)
        assert resultado.expedientes == []
        assert resultado.ficheros_examinados == 0

    def test_sin_base_de_historial_las_llamadas_del_json_salen_igual(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="solo-json")
        assert not ruta_historial(tmp_path).exists()

        assert [e.id_llamada for e in _listar(tmp_path).expedientes] == ["solo-json"]


class TestLosCriterios:
    def test_sin_parametros_no_filtra_nada(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros({})
        assert not criterios.activos()
        assert avisos == []
        assert criterios.tope == TOPE_DEFECTO

    def test_un_nivel_inventado_se_ignora_y_avisa(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros({"nivel": "azul"})
        assert criterios.nivel is None
        assert len(avisos) == 1
        assert "azul" in avisos[0]

    def test_una_cobertura_inventada_se_ignora_y_avisa(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros({"cobertura": "quizas"})
        assert criterios.cobertura is None
        assert avisos

    def test_una_direccion_inventada_se_ignora_y_avisa(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros({"direccion": "saliente"})
        assert criterios.direccion == ""
        assert avisos

    def test_una_fecha_ilegible_se_ignora_y_avisa(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros({"desde": "ayer"})
        assert criterios.desde is None
        assert avisos

    def test_un_rango_al_reves_se_endereza_y_avisa(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros(
            {"desde": "2026-08-20", "hasta": "2026-08-01"}
        )
        assert criterios.desde == date(2026, 8, 1)
        assert criterios.hasta == date(2026, 8, 20)
        assert avisos

    def test_un_limite_fuera_de_la_escalera_cae_al_de_por_defecto(self) -> None:
        for pedido in ("100000", "0", "-5", "cincuenta", ""):
            criterios, avisos = CriteriosExpedientes.desde_parametros({"limite": pedido})
            assert criterios.tope == TOPE_DEFECTO
            assert avisos == [], "el tamaño de página es cosa de la interfaz, no del usuario"

    def test_un_limite_de_la_escalera_se_respeta(self) -> None:
        criterios, _ = CriteriosExpedientes.desde_parametros({"limite": "200"})
        assert criterios.tope == 200

    def test_los_parametros_desconocidos_no_molestan(self) -> None:
        criterios, avisos = CriteriosExpedientes.desde_parametros({"pagina": "3", "orden": "asc"})
        assert not criterios.activos()
        assert avisos == []

    def test_el_tamano_de_pagina_no_cuenta_como_filtro(self) -> None:
        criterios, _ = CriteriosExpedientes.desde_parametros({"limite": "200"})
        assert not criterios.activos()


class TestElTope:
    def test_pasado_el_tope_se_marca_truncado(self, tmp_path: Path) -> None:
        for minuto in range(3):
            _alerta(
                tmp_path,
                f"20260809-12{minuto:02d}00.json",
                id_llamada=f"l-{minuto}",
                momento=f"2026-08-09T12:{minuto:02d}:00",
            )

        resultado = listar_expedientes(tmp_path, CriteriosExpedientes(tope=2))

        assert len(resultado.expedientes) == 2
        assert resultado.truncado
        assert resultado.motivo_truncado == "tope"

    def test_el_tope_de_ficheros_se_marca_aparte(self, tmp_path: Path) -> None:
        for minuto in range(4):
            _alerta(
                tmp_path,
                f"20260809-12{minuto:02d}00.json",
                id_llamada=f"l-{minuto}",
                momento=f"2026-08-09T12:{minuto:02d}:00",
            )

        resultado = listar_expedientes(tmp_path, CriteriosExpedientes(), tope_ficheros=2)

        assert resultado.truncado
        assert resultado.motivo_truncado == "lectura", "es más grave: puede faltar algo antiguo"
        assert resultado.ficheros_examinados == 2


class TestLaTraza:
    def _escribir(self, tmp_path: Path, id_llamada: str, lineas: list[str]) -> None:
        carpeta = dir_trazas(tmp_path)
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / f"{id_llamada}.jsonl").write_text("\n".join(lineas) + "\n", encoding="utf-8")

    def test_lee_las_lineas_del_jsonl(self, tmp_path: Path) -> None:
        self._escribir(
            tmp_path,
            "llamada-a",
            [
                json.dumps(
                    {
                        "momento": "2026-08-09T12:00:00",
                        "consulta": "cuidados de la herida",
                        "motivo": "cubierta:cataratas",
                        "pasajes": [
                            {"origen": "cataratas/guia.pdf", "tema": "cataratas", "distancia": 0.31}
                        ],
                    }
                )
            ],
        )

        (linea,) = leer_traza(tmp_path, "llamada-a")

        assert linea.consulta == "cuidados de la herida"
        assert linea.motivo == "cubierta:cataratas"
        assert linea.pasajes[0].origen == "cataratas/guia.pdf"

    def test_una_linea_a_medias_no_se_lleva_las_anteriores(self, tmp_path: Path) -> None:
        """Se escribe con `append`: un corte de luz deja media línea al final."""
        self._escribir(
            tmp_path,
            "llamada-a",
            [json.dumps({"consulta": "primera", "pasajes": []}), '{"consulta": "seg'],
        )

        lineas = leer_traza(tmp_path, "llamada-a")

        assert [linea.consulta for linea in lineas] == ["primera"]

    def test_una_consulta_sin_pasajes_tambien_es_traza(self, tmp_path: Path) -> None:
        self._escribir(tmp_path, "llamada-a", [json.dumps({"consulta": "algo", "pasajes": []})])
        (linea,) = leer_traza(tmp_path, "llamada-a")
        assert linea.pasajes == []

    def test_un_id_con_barras_no_sale_de_la_carpeta(self, tmp_path: Path) -> None:
        assert leer_traza(tmp_path, "../../etc/passwd") == []
        assert leer_traza(tmp_path, "..") == []

    def test_sin_fichero_devuelve_vacio(self, tmp_path: Path) -> None:
        assert leer_traza(tmp_path, "llamada-inexistente") == []


class TestElExpedienteSuelto:
    def test_junta_la_fila_las_alertas_y_el_resumen(self, tmp_path: Path) -> None:
        _fila(tmp_path, "llamada-20260809-120000", nombre="Nora")
        _alerta(
            tmp_path, "20260809-120000.json", id_llamada="llamada-20260809-120000", nivel="rojo"
        )
        _resumen(tmp_path, "20260809-120500.json", id_llamada="llamada-20260809-120000")

        expediente = leer_expediente(tmp_path, "llamada-20260809-120000")

        assert expediente is not None
        assert expediente.nombre == "Nora"
        assert len(expediente.alertas) == 1
        assert expediente.resumen is not None

    def test_sin_fila_se_encuentra_por_el_dia_del_id(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="llamada-20260809-120000")

        expediente = leer_expediente(tmp_path, "llamada-20260809-120000")

        assert expediente is not None
        assert not expediente.tiene_ficha

    def test_un_id_sin_fecha_dentro_tambien_se_encuentra(self, tmp_path: Path) -> None:
        _alerta(tmp_path, "20260809-120000.json", id_llamada="sin-traza")

        expediente = leer_expediente(tmp_path, "sin-traza")

        assert expediente is not None

    def test_sin_resumen(self, tmp_path: Path) -> None:
        _fila(tmp_path, "llamada-a")
        expediente = leer_expediente(tmp_path, "llamada-a")
        assert expediente is not None
        assert expediente.resumen is None

    def test_un_id_desconocido_es_none(self, tmp_path: Path) -> None:
        assert leer_expediente(tmp_path, "llamada-que-no-existe") is None

    def test_un_id_que_no_es_slug_es_none(self, tmp_path: Path) -> None:
        assert leer_expediente(tmp_path, "../secreto") is None


class TestLasOpcionesDeFiltro:
    def test_junta_los_procedimientos_del_historial_y_los_vistos(self, tmp_path: Path) -> None:
        historial = _fila(tmp_path, "llamada-a")
        historial.anotar_resumen(
            "llamada-a",
            paciente_y_procedimiento="Nora, cataratas",
            decision="",
            proximos_pasos="",
            procedimiento="cataratas",
        )

        opciones = opciones_de_filtro(tmp_path, ["apendicitis", "Cataratas", ""])

        assert opciones.procedimientos == ["apendicitis", "cataratas"], (
            "el del historial gana al visto, y no se duplica por las mayúsculas"
        )

    def test_los_pacientes_salen_del_historial(self, tmp_path: Path) -> None:
        _fila(tmp_path, "llamada-a", numero="3001112233", nombre="Nora")

        opciones = opciones_de_filtro(tmp_path)

        assert [(p.numero, p.nombre, p.total_llamadas) for p in opciones.pacientes] == [
            ("3001112233", "Nora", 1)
        ]

    def test_sin_datos_no_revienta(self, tmp_path: Path) -> None:
        opciones = opciones_de_filtro(tmp_path)
        assert opciones.procedimientos == []
        assert opciones.pacientes == []
