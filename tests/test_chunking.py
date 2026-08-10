"""Tests del troceado de documentos.

El troceado es lógica pura, sin red ni modelos, así que se puede probar a
fondo y rápido. Y conviene hacerlo: un fallo aquí no rompe nada de forma
visible, simplemente degrada la calidad de todas las búsquedas del RAG, que es
la clase de error más cara de descubrir.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from voice_agent.rag.chunking import trocear

TEXTO_LARGO = "\n\n".join(
    f"Este es el párrafo número {i}. Contiene una frase de relleno para dar "
    f"cuerpo al documento y poder comprobar el comportamiento del troceado con "
    f"textos que superan holgadamente el tamaño objetivo configurado."
    for i in range(20)
)


def test_texto_vacio_no_produce_fragmentos() -> None:
    assert trocear("") == []
    assert trocear("   \n\n  ") == []


def test_texto_corto_produce_un_unico_fragmento() -> None:
    fragmentos = trocear("Una frase corta.", tamano=700, solape=120)
    assert len(fragmentos) == 1
    assert fragmentos[0].texto == "Una frase corta."
    assert fragmentos[0].indice == 0


def test_los_fragmentos_respetan_el_tamano_objetivo() -> None:
    fragmentos = trocear(TEXTO_LARGO, tamano=300, solape=50)
    assert len(fragmentos) > 1
    # Se admite un margen: el troceador prefiere no partir palabras antes que
    # ajustarse al carácter exacto.
    for f in fragmentos:
        assert len(f.texto) <= 300 * 1.2, f"fragmento demasiado grande: {len(f.texto)}"


def test_los_indices_son_consecutivos_desde_cero() -> None:
    fragmentos = trocear(TEXTO_LARGO, tamano=300, solape=50)
    assert [f.indice for f in fragmentos] == list(range(len(fragmentos)))


def test_hay_solape_entre_fragmentos_consecutivos() -> None:
    """El solape es lo que evita perder información que cruza un corte."""
    fragmentos = trocear(TEXTO_LARGO, tamano=300, solape=80)
    assert len(fragmentos) >= 2

    # El principio de un fragmento debe aparecer al final del anterior.
    solapes_detectados = 0
    for anterior, siguiente in pairwise(fragmentos):
        inicio = siguiente.texto[:20]
        if inicio and inicio in anterior.texto:
            solapes_detectados += 1
    assert solapes_detectados > 0, "no se detectó solape en ningún par de fragmentos"


def test_los_fragmentos_no_empiezan_a_mitad_de_palabra() -> None:
    """Regresión: el solape cortaba por número de caracteres y partía palabras.

    Un fragmento que empieza por "ra es monofónica..." se lee mal y además
    ensucia la tokenización del modelo de embeddings.
    """
    texto = " ".join(f"palabra{i:03d}" for i in range(400))
    fragmentos = trocear(texto, tamano=200, solape=60)

    for f in fragmentos[1:]:
        primera = f.texto.split()[0]
        assert primera.startswith("palabra"), f"fragmento partido a mitad de palabra: {primera!r}"


def test_el_ultimo_fragmento_no_es_solo_solape() -> None:
    """Regresión: el documento podía terminar con un fragmento ya indexado.

    Si la última pieza provocaba un volcado, quedaba pendiente únicamente la
    cola de solape, que es texto que ya estaba en el fragmento anterior. Eso
    mete un duplicado en el índice que compite con el original en las búsquedas.
    """
    for tamano, solape in ((200, 60), (300, 100), (150, 40)):
        fragmentos = trocear(TEXTO_LARGO, tamano=tamano, solape=solape)
        if len(fragmentos) < 2:
            continue
        ultimo, penultimo = fragmentos[-1].texto, fragmentos[-2].texto
        assert ultimo not in penultimo, (
            f"el último fragmento está contenido en el anterior (tamano={tamano}, solape={solape})"
        )


def test_se_conserva_todo_el_contenido() -> None:
    """Ningún trozo de texto puede desaparecer por el camino."""
    texto = "\n\n".join(f"Marcador{i} contenido del párrafo." for i in range(30))
    fragmentos = trocear(texto, tamano=250, solape=50)
    todo = " ".join(f.texto for f in fragmentos)
    for i in range(30):
        assert f"Marcador{i}" in todo, f"se perdió Marcador{i}"


def test_solape_mayor_o_igual_que_el_tamano_es_un_error() -> None:
    """Con ese ajuste el troceado no avanzaría y entraría en bucle."""
    with pytest.raises(ValueError, match="solape"):
        trocear(TEXTO_LARGO, tamano=100, solape=100)
    with pytest.raises(ValueError, match="solape"):
        trocear(TEXTO_LARGO, tamano=100, solape=150)


def test_prefiere_cortar_por_encabezados_markdown() -> None:
    """Los cortes deberían caer donde un humano los haría.

    Se prueba con `solape=0` a propósito, para aislar el comportamiento del
    troceado del de solapado: con solape, todo fragmento salvo el primero
    empieza por la cola del anterior, así que ninguno podría empezar por "##"
    aunque el corte se hubiera hecho justo ahí.
    """
    texto = (
        "# Título\n\nIntroducción del documento.\n\n"
        "## Primera sección\n\n" + "Contenido de la primera sección. " * 12 + "\n\n"
        "## Segunda sección\n\n" + "Contenido de la segunda sección. " * 12
    )
    fragmentos = trocear(texto, tamano=400, solape=0)

    assert any(f.texto.lstrip().startswith("## ") for f in fragmentos), (
        "ningún fragmento empieza en un encabezado; el troceado no está "
        "respetando los límites semánticos"
    )
