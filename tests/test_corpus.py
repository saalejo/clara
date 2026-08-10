"""La forma del corpus y, sobre todo, la validación de nombres.

Desde que existe la página de Conocimiento, los nombres de temas y documentos
llegan de un formulario web. Estos tests son la red que impide que uno de esos
nombres acabe escribiendo fuera de `corpus/`; el resto comprueba que lo que el
panel enseña y lo que la ingesta indexa son la misma cosa.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voice_agent_core import corpus
from voice_agent_core.corpus import TEMA_RAIZ, ErrorDeCorpus, NombreInvalido


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    raiz = tmp_path / "corpus"
    raiz.mkdir()
    return raiz


#: Todo lo que no puede convertirse en un componente de ruta. Se pasa tanto por
#: la validación estricta como por el slugificado.
NOMBRES_HOSTILES = [
    "..",
    "../",
    "../..",
    "../../etc/passwd",
    "/etc/passwd",
    "/",
    "\\",
    "..\\..\\windows",
    "tema/sub",
    "tema\\sub",
    ".oculto",
    ".",
    "....",
    "",
    "   ",
    "\x00",
    "a\x00b",
    "a\nb",
    "a\tb",
    "x" * 300,
    "%2e%2e/",
    "..%2f..",
    "-empieza-por-guion",
]


# --- Validación estricta -----------------------------------------------------


@pytest.mark.parametrize("nombre", NOMBRES_HOSTILES)
def test_la_validacion_estricta_los_rechaza(nombre: str) -> None:
    with pytest.raises(NombreInvalido):
        corpus.validar_componente(nombre)


@pytest.mark.parametrize("nombre", ["la-placa", "el.agente", "a", "tema_1", "manual.md"])
def test_la_validacion_estricta_acepta_lo_razonable(nombre: str) -> None:
    assert corpus.validar_componente(nombre, maximo=corpus.MAX_LONGITUD_DOCUMENTO) == nombre


def test_la_validacion_no_transforma() -> None:
    # Es lo que permite usarla para BUSCAR y BORRAR: si transformara, un cambio
    # futuro en las reglas dejaría de encontrar carpetas creadas hoy.
    assert corpus.validar_componente("la-placa") == "la-placa"


# --- Slugificado -------------------------------------------------------------


@pytest.mark.parametrize(
    "propuesto,esperado",
    [
        ("Guía de la Placa", "guia-de-la-placa"),
        ("Ñoño Águila", "nono-aguila"),
        ("  espacios  ", "espacios"),
        ("MAYÚSCULAS", "mayusculas"),
        ("con---muchos---guiones", "con-muchos-guiones"),
        ("1. Primer tema", "1-primer-tema"),
    ],
)
def test_normalizar_tema_slugifica(propuesto: str, esperado: str) -> None:
    assert corpus.normalizar_tema(propuesto) == esperado


@pytest.mark.parametrize("propuesto", ["", "   ", "///", "...", "🙂", "\x00"])
def test_normalizar_tema_rechaza_lo_que_no_deja_nada(propuesto: str) -> None:
    with pytest.raises(NombreInvalido):
        corpus.normalizar_tema(propuesto)


@pytest.mark.parametrize("propuesto", NOMBRES_HOSTILES)
def test_la_propiedad_que_sostiene_la_seguridad(propuesto: str) -> None:
    """Para CUALQUIER entrada: o lanza, o devuelve algo que la validación acepta.

    Este es el test que hay que releer en cada revisión de `corpus.py`. Mientras
    se cumpla, ningún nombre venido de un formulario puede componer una ruta que
    se salga del corpus, porque no puede contener siquiera un separador.
    """
    try:
        resultado = corpus.normalizar_tema(propuesto)
    except NombreInvalido:
        return
    assert corpus.validar_componente(resultado) == resultado
    assert "/" not in resultado
    assert "\\" not in resultado
    assert ".." not in resultado


@pytest.mark.parametrize(
    "propuesto,esperado",
    [
        ("Manual de la Placa.md", "manual-de-la-placa.md"),
        ("informe.PDF", "informe.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("NOTAS.MARKDOWN", "notas.markdown"),
    ],
)
def test_normalizar_documento(propuesto: str, esperado: str) -> None:
    assert corpus.normalizar_documento(propuesto) == esperado


@pytest.mark.parametrize("propuesto", ["virus.exe", "sin-extension", "hoja.xlsx", ".md"])
def test_normalizar_documento_exige_una_extension_indexable(propuesto: str) -> None:
    with pytest.raises(NombreInvalido):
        corpus.normalizar_documento(propuesto)


# --- Rutas -------------------------------------------------------------------


@pytest.mark.parametrize("nombre", NOMBRES_HOSTILES)
def test_resolver_rechaza_los_nombres_hostiles(corpus_dir: Path, nombre: str) -> None:
    # En la posición del documento no hay excepciones: ni siquiera la cadena
    # vacía, que como tema significa "la raíz" pero como fichero no significa
    # nada.
    with pytest.raises(NombreInvalido):
        corpus.resolver(corpus_dir, TEMA_RAIZ, nombre)
    if nombre != TEMA_RAIZ:
        with pytest.raises(NombreInvalido):
            corpus.resolver(corpus_dir, nombre)


def test_la_cadena_vacia_como_tema_es_la_raiz(corpus_dir: Path) -> None:
    assert corpus.resolver(corpus_dir, TEMA_RAIZ) == corpus_dir.resolve()


def test_resolver_no_sigue_un_enlace_que_se_sale(corpus_dir: Path, tmp_path: Path) -> None:
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    (afuera / "secreto.md").write_text("no se toca")
    (corpus_dir / "enlace").symlink_to(afuera)

    # El nombre es impecable; lo que falla es a dónde apunta. Esta es la segunda
    # capa, la que sigue en pie aunque la validación de nombres se relaje.
    with pytest.raises(NombreInvalido):
        corpus.resolver(corpus_dir, "enlace", "secreto.md")


def test_resolver_compone_bien_lo_valido(corpus_dir: Path) -> None:
    assert corpus.resolver(corpus_dir, "tema", "a.md") == corpus_dir.resolve() / "tema" / "a.md"
    assert corpus.resolver(corpus_dir, TEMA_RAIZ, "a.md") == corpus_dir.resolve() / "a.md"


# --- Temas -------------------------------------------------------------------


def test_crear_y_borrar_un_tema(corpus_dir: Path) -> None:
    assert corpus.crear_tema(corpus_dir, "La Placa") == "la-placa"
    assert (corpus_dir / "la-placa").is_dir()

    corpus.borrar_tema(corpus_dir, "la-placa")
    assert not (corpus_dir / "la-placa").exists()


def test_no_se_crea_dos_veces(corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")
    with pytest.raises(ErrorDeCorpus):
        corpus.crear_tema(corpus_dir, "la-placa")


def test_no_se_borra_un_tema_con_documentos(corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")
    (corpus_dir / "la-placa" / "a.md").write_text("hola")

    with pytest.raises(ErrorDeCorpus, match="1 fichero"):
        corpus.borrar_tema(corpus_dir, "la-placa")
    # Y sigue todo donde estaba: nada de borrados recursivos desde un formulario.
    assert (corpus_dir / "la-placa" / "a.md").is_file()


def test_el_tema_raiz_no_se_borra(corpus_dir: Path) -> None:
    with pytest.raises(ErrorDeCorpus):
        corpus.borrar_tema(corpus_dir, TEMA_RAIZ)


def test_un_tema_enlazado_fuera_no_se_borra(corpus_dir: Path, tmp_path: Path) -> None:
    """Lo para `resolver`, que es la capa de más abajo."""
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    (corpus_dir / "enlace").symlink_to(afuera)

    with pytest.raises(NombreInvalido, match="fuera del corpus"):
        corpus.borrar_tema(corpus_dir, "enlace")
    assert afuera.is_dir()


def test_un_tema_enlazado_dentro_tampoco_se_borra(corpus_dir: Path) -> None:
    """Este sí llega a la comprobación de `borrar_tema`.

    Un enlace que apunta dentro del corpus resuelve a una ruta legítima, así que
    `resolver` lo deja pasar. El panel no crea enlaces; borrar uno como si fuera
    una carpeta dejaría el destino intacto y confundiría a quien lo hiciera.
    """
    (corpus_dir / "real").mkdir()
    (corpus_dir / "enlace").symlink_to(corpus_dir / "real")

    with pytest.raises(ErrorDeCorpus, match="enlace simbólico"):
        corpus.borrar_tema(corpus_dir, "enlace")
    assert (corpus_dir / "enlace").is_symlink()


# --- Documentos --------------------------------------------------------------


def test_guardar_un_documento(corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "la-placa")
    documento = corpus.guardar_documento(corpus_dir, "la-placa", "Manual.md", [b"hola ", b"mundo"])

    assert documento.nombre == "manual.md"
    assert documento.tema == "la-placa"
    assert documento.ruta_relativa == str(Path("la-placa") / "manual.md")
    assert (corpus_dir / "la-placa" / "manual.md").read_bytes() == b"hola mundo"


def test_guardar_en_la_raiz(corpus_dir: Path) -> None:
    documento = corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "suelto.md", [b"x"])
    assert documento.tema == TEMA_RAIZ
    assert documento.ruta_relativa == "suelto.md"


def test_no_se_sube_a_un_tema_inexistente(corpus_dir: Path) -> None:
    with pytest.raises(ErrorDeCorpus, match="no existe"):
        corpus.guardar_documento(corpus_dir, "inventado", "a.md", [b"x"])
    assert not (corpus_dir / "inventado").exists()


def test_no_se_sobrescribe(corpus_dir: Path) -> None:
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"original"])
    with pytest.raises(ErrorDeCorpus, match="Ya hay"):
        corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"impostor"])
    assert (corpus_dir / "a.md").read_bytes() == b"original"


def test_pasarse_del_tope_no_deja_nada_a_medias(corpus_dir: Path) -> None:
    grande = [b"x" * 1024] * ((corpus.MAX_BYTES_DOCUMENTO // 1024) + 2)
    with pytest.raises(ErrorDeCorpus, match="máximo"):
        corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "gordo.md", grande)

    # Ni el documento ni el temporal: un .tmp abandonado ocuparía disco para
    # siempre en una placa con microSD.
    assert list(corpus_dir.iterdir()) == []


def test_un_documento_vacio_se_rechaza(corpus_dir: Path) -> None:
    with pytest.raises(ErrorDeCorpus, match="vacío"):
        corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "nada.md", [])
    assert list(corpus_dir.iterdir()) == []


def test_borrar_un_documento(corpus_dir: Path) -> None:
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"x"])
    corpus.borrar_documento(corpus_dir, TEMA_RAIZ, "a.md")
    assert not (corpus_dir / "a.md").exists()


def test_no_se_borra_a_traves_de_un_enlace_que_sale(corpus_dir: Path, tmp_path: Path) -> None:
    fuera = tmp_path / "importante.md"
    fuera.write_text("no se toca")
    (corpus_dir / "enlace.md").symlink_to(fuera)

    with pytest.raises(NombreInvalido, match="fuera del corpus"):
        corpus.borrar_documento(corpus_dir, TEMA_RAIZ, "enlace.md")
    assert fuera.is_file()


def test_no_se_borra_lo_que_no_es_un_documento(corpus_dir: Path) -> None:
    (corpus_dir / "real.md").write_text("x")
    (corpus_dir / "enlace.md").symlink_to(corpus_dir / "real.md")
    (corpus_dir / "hoja.xlsx").write_text("x")

    for nombre in ("enlace.md", "hoja.xlsx", "no-existe.md"):
        with pytest.raises(ErrorDeCorpus):
            corpus.borrar_documento(corpus_dir, TEMA_RAIZ, nombre)
    assert (corpus_dir / "real.md").is_file()


# --- Inventario y descubrimiento ---------------------------------------------


def test_el_inventario_pone_la_raiz_primero(corpus_dir: Path) -> None:
    corpus.crear_tema(corpus_dir, "zeta")
    corpus.crear_tema(corpus_dir, "alfa")
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "suelto.md", [b"x"])
    corpus.guardar_documento(corpus_dir, "alfa", "uno.md", [b"x"])

    temas = corpus.inventario(corpus_dir)

    assert [t.nombre for t in temas] == [TEMA_RAIZ, "alfa", "zeta"]
    assert temas[0].es_raiz
    assert [d.nombre for d in temas[0].documentos] == ["suelto.md"]
    assert [d.nombre for d in temas[1].documentos] == ["uno.md"]


def test_el_inventario_ignora_ocultos_y_enlaces(corpus_dir: Path, tmp_path: Path) -> None:
    (corpus_dir / ".oculto.md").write_text("x")
    (corpus_dir / "notas.txt").write_text("x")
    (corpus_dir / "enlace.md").symlink_to(tmp_path / "lo-que-sea.md")

    nombres = [d.nombre for d in corpus.inventario(corpus_dir)[0].documentos]
    assert nombres == ["notas.txt"]


def test_lo_que_lista_el_panel_es_lo_que_indexa_la_ingesta(corpus_dir: Path) -> None:
    """La propiedad que evita el 'lo subí y el agente no lo conoce'."""
    corpus.crear_tema(corpus_dir, "la-placa")
    corpus.guardar_documento(corpus_dir, "la-placa", "a.md", [b"x"])
    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "b.md", [b"x"])

    del_inventario = {
        d.ruta_relativa for tema in corpus.inventario(corpus_dir) for d in tema.documentos
    }
    de_la_ingesta = {
        str(r.relative_to(corpus_dir)) for r in corpus.descubrir_documentos(corpus_dir)
    }
    assert del_inventario == de_la_ingesta


def test_descubrir_no_baja_mas_de_un_nivel(corpus_dir: Path) -> None:
    hondo = corpus_dir / "tema" / "subtema"
    hondo.mkdir(parents=True)
    (hondo / "perdido.md").write_text("x")

    assert corpus.descubrir_documentos(corpus_dir) == []
    # Pero no se ignora en silencio: la ingesta puede avisar de él.
    assert corpus.documentos_ignorados(corpus_dir) == [hondo / "perdido.md"]


def test_funciona_con_un_corpus_dir_relativo(
    corpus_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso de producción: `make panel` corre con CORPUS_DIR='corpus'.

    `resolver` devuelve rutas absolutas, así que calcular la ruta relativa de un
    documento contra un `corpus_dir` sin resolver reventaba con un
    "is not in the subpath of 'corpus'" que no señalaba a ningún sitio.
    """
    corpus.crear_tema(corpus_dir, "la-placa")
    corpus.guardar_documento(corpus_dir, "la-placa", "a.md", [b"x"])
    monkeypatch.chdir(corpus_dir.parent)

    relativo = Path("corpus")
    documentos = corpus.inventario(relativo)[1].documentos

    assert [d.ruta_relativa for d in documentos] == [str(Path("la-placa") / "a.md")]
    assert corpus.descubrir_documentos(relativo) == [relativo / "la-placa" / "a.md"]


def test_descubrir_sin_corpus_falla_con_un_mensaje_util(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No existe la carpeta del corpus"):
        corpus.descubrir_documentos(tmp_path / "no-esta")


# --- Marca de cambio ---------------------------------------------------------


def _envejecer(ruta: Path, segundos: float = 10.0) -> None:
    """Retrasa la fecha de una ruta, para no depender de la resolución del reloj."""
    estado = ruta.stat()
    os.utime(ruta, (estado.st_atime - segundos, estado.st_mtime - segundos))


def test_la_marca_sube_al_crear_un_documento(corpus_dir: Path) -> None:
    _envejecer(corpus_dir)
    antes = corpus.marca_de_cambio(corpus_dir)

    corpus.guardar_documento(corpus_dir, TEMA_RAIZ, "a.md", [b"x"])

    assert corpus.marca_de_cambio(corpus_dir) > antes


def test_la_marca_sube_al_borrar_un_documento(corpus_dir: Path) -> None:
    """El caso que solo detecta la fecha del directorio.

    Al borrar no queda ningún fichero cuya fecha haya cambiado; la única pista es
    el mtime de la carpeta que lo contenía. Sin mirarlo, el panel diría que el
    índice está al día justo después de un borrado.
    """
    corpus.crear_tema(corpus_dir, "tema")
    corpus.guardar_documento(corpus_dir, "tema", "a.md", [b"x"])
    for ruta in (corpus_dir, corpus_dir / "tema", corpus_dir / "tema" / "a.md"):
        _envejecer(ruta)
    antes = corpus.marca_de_cambio(corpus_dir)

    corpus.borrar_documento(corpus_dir, "tema", "a.md")

    assert corpus.marca_de_cambio(corpus_dir) > antes


def test_la_marca_sube_al_crear_y_borrar_temas(corpus_dir: Path) -> None:
    _envejecer(corpus_dir)
    antes = corpus.marca_de_cambio(corpus_dir)
    corpus.crear_tema(corpus_dir, "tema")
    assert corpus.marca_de_cambio(corpus_dir) > antes

    for ruta in (corpus_dir, corpus_dir / "tema"):
        _envejecer(ruta)
    antes_de_borrar = corpus.marca_de_cambio(corpus_dir)
    corpus.borrar_tema(corpus_dir, "tema")
    assert corpus.marca_de_cambio(corpus_dir) > antes_de_borrar


def test_la_marca_de_un_corpus_inexistente_es_cero(tmp_path: Path) -> None:
    assert corpus.marca_de_cambio(tmp_path / "no-esta") == 0.0
