"""Tests del filtrado y la fusión del buscador del RAG.

Lo que se prueba aquí es la lógica de decisión —qué pasajes se aceptan, cómo se
fusionan los de varios temas y cómo se presentan al modelo—, no la calidad de
los embeddings. Por eso se usan dobles de ChromaDB: los tests corren en
milisegundos, sin descargar modelos ni depender de la red, y siguen cubriendo la
parte donde de verdad se cometen errores.

La calidad de la recuperación es otra cosa y se mide aparte, con el corpus real;
`docs/rag.md` explica cómo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voice_agent.rag import retriever as modulo
from voice_agent.rag.retriever import Retriever
from voice_agent.rag.store import nombre_coleccion
from voice_agent_core.config import Settings
from voice_agent_core.corpus import TEMA_RAIZ

#: (texto, origen, distancia)
Fila = tuple[str, str, float]


class ColeccionFalsa:
    """Doble de una colección de ChromaDB."""

    def __init__(self, name: str, tema: str, filas: list[Fila], *, con_tema: bool = True) -> None:
        # `name` es el nombre completo de la colección (con su prefijo), que es
        # lo que ve `temas_indexados`; `tema` es lo que va en los metadatos.
        self.name = name
        self._tema = tema
        self._filas = filas
        self._con_tema = con_tema
        self.consultas: list[Any] = []

    def count(self) -> int:
        return len(self._filas)

    def query(
        self, *, query_embeddings: list[Any], n_results: int, include: Any = None
    ) -> dict[str, Any]:
        self.consultas.append(query_embeddings[0])
        recorte = self._filas[:n_results]
        return {
            "documents": [[t for t, _, _ in recorte]],
            "metadatas": [
                [
                    # Un índice construido antes de que existieran los temas no
                    # trae la clave, y el buscador no puede reventar por eso.
                    {"origen": o, "tema": self._tema} if self._con_tema else {"origen": o}
                    for _, o, _ in recorte
                ]
            ],
            "distances": [[d for _, _, d in recorte]],
        }


class ClienteFalso:
    """Doble del cliente persistente."""

    def __init__(self, colecciones: dict[str, ColeccionFalsa]) -> None:
        self.colecciones = colecciones

    def list_collections(self) -> list[ColeccionFalsa]:
        return list(self.colecciones.values())

    def get_or_create_collection(self, *, name: str, **_: Any) -> ColeccionFalsa:
        return self.colecciones[name]


class EmbeddingsFalsos:
    """Doble del modelo de embeddings, que cuenta cuántas veces lo llaman."""

    def __init__(self) -> None:
        self.llamadas: list[str] = []

    def embed_query(self, input: list[str]) -> list[Any]:
        self.llamadas.extend(input)
        return [[0.0, 1.0]]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, rag_max_distance=0.68, rag_top_k=4)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def embeddings(monkeypatch: pytest.MonkeyPatch) -> EmbeddingsFalsos:
    """Nadie carga el modelo de verdad.

    Hay que sustituirlo en los **tres** sitios: el buscador lo pide por su cuenta
    para vectorizar la pregunta, `store.abrir_coleccion` lo pide otra vez para
    dárselo a la colección, y desde que la función de embeddings carga el modelo
    en el primer uso el buscador llama además a `cargar_modelo` para calentarlo
    al arrancar. Dejarse uno hace que la batería tarde el doble y baje 120 MB de
    modelo — que es justo lo que este proyecto no quiere de sus tests.
    """
    falsos = EmbeddingsFalsos()
    monkeypatch.setattr(modulo, "funcion_embeddings", lambda *a, **k: falsos)
    monkeypatch.setattr(modulo, "cargar_modelo", lambda *a, **k: None)
    monkeypatch.setattr("voice_agent.rag.store.funcion_embeddings", lambda *a, **k: falsos)
    return falsos


def _montar(
    settings: Settings,
    por_tema: dict[str, list[Fila]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    con_tema: bool = True,
) -> tuple[Retriever, ClienteFalso]:
    """Construye un Retriever sobre un cliente falso con las colecciones dadas.

    Solo se sustituyen el cliente y el modelo: `temas_indexados` y
    `abrir_coleccion` son los de verdad, así que el reparto por temas y el
    prefijo de las colecciones se prueban de paso.
    """
    colecciones = {
        nombre_coleccion(settings, tema): ColeccionFalsa(
            nombre_coleccion(settings, tema), tema, filas, con_tema=con_tema
        )
        for tema, filas in por_tema.items()
    }
    cliente = ClienteFalso(colecciones)
    monkeypatch.setattr(modulo, "abrir_cliente", lambda *a, **k: cliente)
    return Retriever(settings, exigir_indice=False), cliente


def _retriever(settings: Settings, filas: list[Fila], monkeypatch: pytest.MonkeyPatch) -> Retriever:
    """Atajo para los casos de un solo tema."""
    return _montar(settings, {TEMA_RAIZ: filas}, monkeypatch)[0]


# --- Filtrado por distancia --------------------------------------------------


def test_descarta_los_pasajes_por_encima_del_umbral(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """El filtro por distancia es la defensa principal contra las alucinaciones."""
    pasajes = _retriever(
        settings,
        [
            ("Muy relevante", "a.md", 0.30),
            ("Algo relevante", "b.md", 0.60),
            ("Irrelevante", "c.md", 0.69),  # justo por encima de 0.68
            ("Nada que ver", "d.md", 0.95),
        ],
        monkeypatch,
    ).buscar("una pregunta")

    assert [p.texto for p in pasajes] == ["Muy relevante", "Algo relevante"]


def test_conserva_el_origen_para_poder_citar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    pasajes = _retriever(settings, [("Contenido", "manual/placa.md", 0.20)], monkeypatch).buscar(
        "algo"
    )

    assert pasajes[0].origen == "manual/placa.md"


def test_la_similitud_es_el_complemento_de_la_distancia(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    pasajes = _retriever(settings, [("Contenido", "a.md", 0.25)], monkeypatch).buscar("algo")

    assert pasajes[0].similitud == pytest.approx(0.75)


def test_respeta_el_limite_top_k(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    filas = [(f"Pasaje {i}", "a.md", 0.10) for i in range(10)]
    pasajes = _retriever(settings, filas, monkeypatch).buscar("algo", top_k=3)

    assert len(pasajes) == 3


def test_una_consulta_vacia_no_llega_a_buscar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, cliente = _montar(settings, {TEMA_RAIZ: [("Contenido", "a.md", 0.10)]}, monkeypatch)

    assert retriever.buscar("   ") == []
    assert embeddings.llamadas == []
    assert cliente.colecciones["conocimiento"].consultas == []


def test_un_indice_vacio_devuelve_lista_vacia_sin_reventar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(settings, {}, monkeypatch)
    assert retriever.buscar("algo") == []


# --- Fusión de varios temas --------------------------------------------------


def test_fusiona_los_temas_ordenando_por_distancia(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """El resultado no depende de cómo estén repartidos los documentos.

    Es la propiedad que permite trocear el índice por temas sin cambiar lo que el
    agente recupera, y por tanto sin invalidar el umbral medido.
    """
    retriever, _ = _montar(
        settings,
        {
            TEMA_RAIZ: [("Raíz media", "r.md", 0.40)],
            "la-placa": [("Placa cercana", "p.md", 0.15), ("Placa lejana", "p2.md", 0.66)],
            "el-agente": [("Agente cercano", "a.md", 0.25)],
        },
        monkeypatch,
    )

    pasajes = retriever.buscar("algo")

    assert [p.texto for p in pasajes] == [
        "Placa cercana",
        "Agente cercano",
        "Raíz media",
        "Placa lejana",
    ]


def test_el_top_k_es_global_no_por_tema(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(
        settings,
        {
            "uno": [("a", "a.md", 0.10), ("b", "b.md", 0.11)],
            "dos": [("c", "c.md", 0.12), ("d", "d.md", 0.13)],
            "tres": [("e", "e.md", 0.14), ("f", "f.md", 0.15)],
        },
        monkeypatch,
    )

    assert len(retriever.buscar("algo", top_k=2)) == 2


def test_la_pregunta_se_vectoriza_una_sola_vez(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Lo caro de una consulta es el embedding, no recorrer el índice.

    Si esto se rompiera, cada tema nuevo añadiría el coste entero de vectorizar
    la pregunta, y el fan-out dejaría de salir gratis.
    """
    retriever, cliente = _montar(
        settings,
        {TEMA_RAIZ: [("a", "a.md", 0.1)], "uno": [("b", "b.md", 0.1)], "dos": [("c", "c.md", 0.1)]},
        monkeypatch,
    )

    retriever.buscar("una pregunta")

    assert embeddings.llamadas == ["una pregunta"]
    # Y el mismo vector ha llegado a las tres colecciones.
    assert all(c.consultas == [[0.0, 1.0]] for c in cliente.colecciones.values())


def test_cada_pasaje_sabe_de_que_tema_viene(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(
        settings,
        {TEMA_RAIZ: [("Suelto", "s.md", 0.20)], "la-placa": [("De la placa", "p.md", 0.10)]},
        monkeypatch,
    )

    pasajes = retriever.buscar("algo")

    assert [(p.tema, p.texto) for p in pasajes] == [
        ("la-placa", "De la placa"),
        (TEMA_RAIZ, "Suelto"),
    ]


def test_un_indice_sin_metadato_de_tema_no_revienta(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Los fragmentos indexados antes de que existieran los temas siguen valiendo."""
    retriever, _ = _montar(
        settings, {TEMA_RAIZ: [("Antiguo", "a.md", 0.20)]}, monkeypatch, con_tema=False
    )

    pasajes = retriever.buscar("algo")

    assert pasajes[0].tema == ""
    assert pasajes[0].origen == "a.md"


def test_un_tema_nuevo_aparece_sin_reconstruir_el_retriever(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Reindexar desde el panel no reinicia el agente.

    Si el buscador se quedara con la lista de colecciones del arranque, subir un
    documento a un tema nuevo y reindexar no serviría de nada hasta el siguiente
    reinicio, y el agente juraría no saber nada de él.
    """
    retriever, cliente = _montar(settings, {TEMA_RAIZ: [("Viejo", "v.md", 0.20)]}, monkeypatch)
    assert len(retriever.buscar("algo")) == 1

    cliente.colecciones["conocimiento__nuevo"] = ColeccionFalsa(
        "conocimiento__nuevo", "nuevo", [("Recién indexado", "n.md", 0.10)]
    )

    pasajes = retriever.buscar("algo")
    assert [p.texto for p in pasajes] == ["Recién indexado", "Viejo"]


def test_un_tema_borrado_deja_de_consultarse(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, cliente = _montar(
        settings,
        {TEMA_RAIZ: [("Queda", "q.md", 0.20)], "se-va": [("Desaparece", "d.md", 0.10)]},
        monkeypatch,
    )
    assert len(retriever.buscar("algo")) == 2

    del cliente.colecciones["conocimiento__se-va"]

    assert [p.texto for p in retriever.buscar("algo")] == ["Queda"]


def test_num_fragmentos_suma_todos_los_temas(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    retriever, _ = _montar(
        settings,
        {TEMA_RAIZ: [("a", "a.md", 0.1)], "uno": [("b", "b.md", 0.1), ("c", "c.md", 0.1)]},
        monkeypatch,
    )

    assert retriever.num_fragmentos == 3


def test_exigir_indice_falla_si_no_hay_nada(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    monkeypatch.setattr(modulo, "abrir_cliente", lambda *a, **k: ClienteFalso({}))

    with pytest.raises(FileNotFoundError, match="make ingest"):
        Retriever(settings)


# --- Restricción por tema ----------------------------------------------------
#
# El umbral de distancia mide parecido con la consulta, no pertenencia a una
# cirugía: medido en la placa, «cuidados de la herida cirugia de cataratas ojo»
# recuperaba cinco pasajes de colecistitis y de reemplazo articular por debajo
# del umbral. Restringir por tema es lo que cierra esa puerta, así que estos
# tests vigilan sobre todo que no se pueda escapar por ninguna rendija.


def _tres_temas(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[Retriever, ClienteFalso]:
    return _montar(
        settings,
        {
            TEMA_RAIZ: [("General", "g.md", 0.30)],
            "colecistitis": [("De la vesícula", "c.pdf", 0.20)],
            "apendicitis": [("Del apéndice", "a.pdf", 0.10)],
        },
        monkeypatch,
    )


def test_restringir_no_llega_a_consultar_las_demas_colecciones(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """No basta con filtrar los pasajes después: no se consultan siquiera.

    Filtrar a posteriori daría el mismo resultado visible y sería un error
    silencioso el día que alguien añada un reordenamiento por el camino.
    """
    retriever, cliente = _tres_temas(settings, monkeypatch)

    pasajes = retriever.buscar("algo", temas=["colecistitis"])

    assert [p.texto for p in pasajes] == ["De la vesícula"]
    assert cliente.colecciones["conocimiento__apendicitis"].consultas == []
    assert cliente.colecciones["conocimiento"].consultas == []


def test_el_tema_raiz_se_puede_pedir_junto_al_de_la_cirugia(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Los documentos sueltos son material general y acompañan a cualquiera."""
    retriever, _ = _tres_temas(settings, monkeypatch)

    pasajes = retriever.buscar("algo", temas=["colecistitis", TEMA_RAIZ])

    assert [p.texto for p in pasajes] == ["De la vesícula", "General"]


def test_restringir_a_un_tema_que_no_existe_no_cae_a_buscar_en_todos(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """El test que impide el fallback más tentador y más peligroso.

    "Si el tema pedido no está, busca en todos" parece un apaño amable y es
    justo el fallo original: el paciente de cataratas recibiría los protocolos
    de la vesícula. Además se comprueba que ni siquiera se vectoriza la
    pregunta, porque el corte tiene que ocurrir antes de gastar el embedding.
    """
    retriever, cliente = _tres_temas(settings, monkeypatch)
    embeddings.llamadas.clear()

    assert retriever.buscar("algo", temas=["cataratas"]) == []
    assert embeddings.llamadas == []
    assert all(c.consultas == [] for c in cliente.colecciones.values())


def test_sin_restringir_se_buscan_todos_los_temas(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """No regresión: `temas=None` es el comportamiento de siempre."""
    retriever, _ = _tres_temas(settings, monkeypatch)

    assert len(retriever.buscar("algo")) == 3


def test_restringir_no_deja_al_retriever_sin_temas_para_la_siguiente(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """`_colecciones()` devuelve su diccionario por referencia.

    Filtrarlo en sitio en vez de construir uno nuevo dejaría al buscador con un
    solo tema para el resto de la llamada, y el síntoma sería que el segundo
    paciente no encuentra nada de su cirugía.
    """
    retriever, _ = _tres_temas(settings, monkeypatch)

    retriever.buscar("algo", temas=["colecistitis"])

    assert len(retriever.buscar("algo")) == 3


def test_un_tema_recien_indexado_se_puede_restringir_sin_reiniciar(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
) -> None:
    """Subir la guía de una cirugía nueva la cubre en mitad de una llamada."""
    retriever, cliente = _tres_temas(settings, monkeypatch)
    assert retriever.buscar("algo", temas=["cataratas"]) == []

    cliente.colecciones["conocimiento__cataratas"] = ColeccionFalsa(
        "conocimiento__cataratas", "cataratas", [("Del ojo", "o.pdf", 0.05)]
    )

    assert [p.texto for p in retriever.buscar("algo", temas=["cataratas"])] == ["Del ojo"]
    assert "cataratas" in retriever.temas_disponibles()


# --- Formato para el modelo --------------------------------------------------


class TestFormatoParaElModelo:
    def test_incluye_numeracion_y_fuente(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
    ) -> None:
        texto = _retriever(
            settings,
            [("Primer pasaje", "uno.md", 0.20), ("Segundo pasaje", "dos.md", 0.30)],
            monkeypatch,
        ).buscar_como_texto("algo")

        assert "[1]" in texto and "[2]" in texto
        assert "uno.md" in texto and "dos.md" in texto
        assert "Primer pasaje" in texto and "Segundo pasaje" in texto

    def test_sin_resultados_devuelve_una_instruccion_explicita(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, embeddings: EmbeddingsFalsos
    ) -> None:
        """Nunca una cadena vacía: el modelo la leería como un fallo de la herramienta."""
        texto = _retriever(
            settings, [("Irrelevante", "a.md", 0.99)], monkeypatch
        ).buscar_como_texto("algo")

        assert texto.strip()
        assert "no contiene información relevante" in texto
        assert "no te inventes" in texto
