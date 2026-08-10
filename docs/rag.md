# La base de conocimiento (RAG)

RAG significa *retrieval-augmented generation*: en lugar de meter todo lo que el
agente debe saber en el prompt del sistema, se indexa en una base de datos
vectorial y el modelo lo consulta **cuando lo necesita**, usando una herramienta.

## Uso rápido

Lo normal es hacerlo desde el navegador, en la página **Conocimiento** del panel
(ver `panel.md`): crear temas, subir documentos y darle a reindexar, sin entrar
por SSH. Por línea de órdenes:

```bash
# 1. Mete tus documentos (.md, .txt, .pdf) en corpus/, agrupados por temas
mkdir corpus/manuales && cp mis-manuales/*.md corpus/manuales/

# 2. Indexa
make ingest          # reconcilia el índice con el corpus
make reingest        # lo reconstruye desde cero

# 3. Prueba la recuperación sin pasar por el micrófono
make ask Q="cuántos núcleos tiene la placa"
```

Trabajar por línea de órdenes es la forma correcta de afinar el RAG: separa el
problema de recuperación del de conversación. Si `make ask` no encuentra lo que
buscas, no tiene sentido probarlo hablando.

## Los temas: una carpeta y una colección

Cada subcarpeta de `corpus/` es un **tema**, y cada tema tiene su **propia
colección en ChromaDB**. Los documentos sueltos en la raíz forman el tema sin
nombre:

```
corpus/como-funciona-el-agente.md  ←→  colección  conocimiento
corpus/la-placa/*.md               ←→  colección  conocimiento__la-placa
```

El corpus tiene **exactamente dos niveles**. Un documento más hondo no se indexa,
y tanto la ingesta como el panel lo dicen en vez de ignorarlo en silencio.

Repartir el índice no es orden por el orden: es lo que hace que se pueda
mantener. Con una sola colección, olvidar algo obligaba a reconstruirlo todo.

| | Una colección | Una por tema |
|---|---|---|
| Borrar un tema | `--reset` y reindexar entero | se elimina su colección |
| Borrar un documento | `--reset` y reindexar entero | se quitan sus fragmentos |
| Reindexar tras un cambio | todo | solo lo que cambió |

**El agente no se entera de nada de esto.** El buscador vectoriza la pregunta una
sola vez y consulta con ese mismo vector todas las colecciones, fusiona por
distancia y se queda con los `top_k` mejores del conjunto. Como la distancia de
un fragmento a la pregunta no depende de con quién comparta colección, el
resultado es idéntico al de un índice único —medido: "cuántos núcleos tiene la
placa" sigue dando 0.602, el mismo valor de la tabla de calibración de más
abajo— y el umbral no hay que recalibrarlo.

Los nombres de tema se limitan a 40 caracteres y al alfabeto `[a-z0-9._-]`
porque acaban formando el de una colección, y ChromaDB solo admite de 3 a 63
caracteres de `[a-zA-Z0-9._-]` empezando y acabando en alfanumérico. Un test
comprueba que todo tema que el panel deja crear produce un nombre válido, para
que el fallo no aparezca a mitad de una indexación.

## Cómo funciona

```
corpus/<tema>/*.md
    │
    ├─ leer_documento()      texto plano (pypdf si es PDF)
    ├─ trocear()             fragmentos de ~700 caracteres con 120 de solape
    ├─ contextualizar()      antepone el título del documento a cada fragmento
    ├─ FastEmbedEmbedding    cada fragmento -> vector de 384 dimensiones
    └─ coleccion.upsert()    la colección del tema, distancia coseno
                                    │
consulta ──► vector ──► k vecinos por colección ──► fusión ──► filtro ──► LLM
```

El **tema no entra en el texto que se vectoriza**. Viaja como metadato y como
prefijo de `origen`, pero `contextualizar()` no lo toca a propósito: el umbral
de la tabla de calibración está medido sobre exactamente esa cadena, y meterlo
dentro obligaría a repetir la medición entera.

### El troceado

Un modelo de embeddings comprime cada fragmento en un solo vector, así que el
tamaño del fragmento decide la calidad de todo lo demás. Demasiado grande y el
vector mezcla temas y se vuelve un promedio difuso que no se parece a ninguna
pregunta concreta; demasiado pequeño y el fragmento pierde el contexto que lo
hacía comprensible.

`rag/chunking.py` implementa un troceado **recursivo por separadores**: corta
primero por los límites más semánticos que encuentre (encabezados `##`, luego
párrafos, luego líneas, luego frases) y solo parte por palabras cuando no queda
otra. Así los cortes caen casi siempre donde los haría una persona.

El **solape** de 120 caracteres hace que una frase que caiga justo en la frontera
entre dos fragmentos aparezca completa al menos en uno. La cola de solape se
ajusta a un límite de palabra: cortar por número de caracteres a secas produce
fragmentos que empiezan por "ra es monofónica...", que se leen mal y ensucian la
tokenización.

Está escrito a mano, en unas pocas decenas de líneas, en vez de traer LangChain.
Es menos dependencia, se lee de un vistazo y —lo importante— se puede probar:
`tests/test_chunking.py`.

### El troceado contextual

Cada fragmento se indexa precedido del **título de su documento**:

```
Cómo funciona este agente de voz

Todo se empaqueta en un contenedor que se ejecuta con Podman...
```

La razón es concreta. El párrafo sobre el empaquetado no contiene en ningún
sitio la palabra "agente", así que el vector de la pregunta "¿cómo se despliega
el agente?" no acababa lo bastante cerca. Anteponer el título codifica en el
vector de qué documento habla, y además el modelo que luego lo lee sabe de dónde
salió. Cuesta cero y mejora la recuperación de forma medible.

### Los embeddings

`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, ejecutado por
**fastembed** sobre ONNX Runtime. 384 dimensiones, multilingüe, unos 120 MB.

Deliberadamente **no** se usa `sentence-transformers`, que sería lo habitual,
porque arrastra PyTorch: casi mil megabytes en aarch64. Tampoco se usa el
`all-MiniLM-L6-v2` que ChromaDB trae por defecto, porque está entrenado
esencialmente en inglés.

Si quieres más calidad a costa de velocidad, `jinaai/jina-embeddings-v2-base-es`
está especializado en español. Cambiar `EMBEDDING_MODEL` **obliga a reindexar**
(`make reingest`): un índice construido con un modelo y consultado con otro
devuelve resultados sin ningún sentido.

### La ingesta reconcilia, no acumula

El identificador de cada fragmento se deriva de la ruta del documento, el número
de fragmento y un resumen SHA-256 de su contenido. Volver a ejecutar `make
ingest` sobre un corpus sin cambios no duplica nada; sobre un documento editado,
sustituye solo los fragmentos afectados.

Y además **olvida**. Por cada tema, la ingesta:

1. vuelca sus documentos en la colección del tema,
2. borra de esa colección los identificadores que no ha vuelto a escribir —los
   de documentos borrados y los de versiones anteriores de uno editado—,
3. y al terminar elimina las colecciones de los temas cuya carpeta ya no está.

Durante mucho tiempo esto no se podía hacer: con un índice único, una ingesta
incremental no puede olvidar un documento borrado porque ya no hay nada que
recorrer, y la única salida era reconstruirlo todo. Con cada tema en su propia
colección sí se puede, porque se compara la colección contra su carpeta, que sí
se recorre entera.

`make reingest` (o `--reset`) sigue existiendo, pero ya solo hace falta cuando
cambia algo que invalida los vectores en sí: `EMBEDDING_MODEL`, `CHUNK_SIZE`,
`CHUNK_OVERLAP` o `CHROMA_COLLECTION`.

### El modelo se carga una sola vez

ChromaDB guarda en los metadatos de cada colección con qué función de embeddings
se creó, y la **reconstruye por su cuenta** llamando a `build_from_config`. O sea
que cuántas veces se instancia `FastEmbedEmbeddingFunction` no lo decide nuestro
código: sale de cuántas colecciones se abren y cuántas operaciones se hacen.

Medido en la placa: sin cachear, indexar 11 fragmentos cargaba el modelo **seis
veces** y tardaba 43 s. Con la caché de `cargar_modelo`, una vez y 16 s. Por eso
el `lru_cache` está en `embeddings.py`, sobre el modelo, y no sobre el envoltorio
que Chroma se empeña en rehacer.

## El filtro por distancia: lo que evita las alucinaciones

Una búsqueda vectorial **siempre** devuelve los `k` vecinos más cercanos, aunque
el más cercano no tenga nada que ver con la pregunta. Si eso se le pasa al modelo
tal cual, el modelo construye una respuesta a partir de contexto irrelevante:
es la causa más común de alucinación en un RAG.

Por eso se descarta todo lo que supere `RAG_MAX_DISTANCE` (distancia coseno,
donde 0 es idéntico y 2 es opuesto). Cuando no queda ningún pasaje, la
herramienta devuelve una instrucción explícita —"la base de conocimiento no
contiene información relevante, dilo con naturalidad y no te inventes la
respuesta"— en lugar de una cadena vacía, que el modelo interpretaría como un
fallo de la herramienta.

## Cómo calibrar el umbral para tu corpus

El valor por defecto, **0.68**, sale de medir el corpus de ejemplo. Para otro
corpus hay que repetir la medida: escribe una lista de preguntas que tu corpus
**sí** cubre y otra de preguntas que **no**, y mira dónde caen.

```python
from voice_agent.config import get_settings
from voice_agent.rag.store import abrir_coleccion

s = get_settings()
s.apply_model_cache_env()
col = abrir_coleccion(s)

DENTRO = ["...preguntas que tu corpus responde..."]
FUERA = ["...preguntas de temas ajenos..."]

d = lambda q: col.query(query_texts=[q], n_results=1, include=["distances"])["distances"][0][0]

print("peor caso cubierto:  ", max(d(q) for q in DENTRO))
print("mejor caso NO cubierto:", min(d(q) for q in FUERA))
```

El umbral va entre esos dos números. Estos son los resultados con el corpus de
ejemplo:

| Preguntas cubiertas | Distancia | | Preguntas no cubiertas | Distancia |
|---|---|---|---|---|
| qué voz usa para hablar | 0.276 | | receta de la paella | 0.693 |
| qué modelo de whisper se usa | 0.341 | | billete de tren a Sevilla | 0.820 |
| cuánta memoria RAM tiene | 0.388 | | mundial de fútbol de 2010 | 0.842 |
| cuánto tarda en transcribir | 0.441 | | recomiéndame una película | 0.857 |
| por qué no puede usar 16 kHz | 0.481 | | cambiar el aceite del coche | 0.895 |
| puedo interrumpir al agente | 0.485 | | capital de Mongolia | 0.935 |
| cuántos núcleos tiene la placa | 0.602 | | | |
| qué base de datos vectorial usa | 0.623 | | | |
| qué sistema operativo corre | 0.632 | | | |
| cómo se despliega | 0.726 | | | |

**Los rangos se solapan**: el peor caso cubierto (0.726) queda por encima del
mejor caso no cubierto (0.693). No existe un umbral que los separe
perfectamente, y conviene saberlo en vez de creer que el filtro es infalible.

Con 0.68 se rechazan las seis preguntas ajenas y se pierde una de las diez
cubiertas. Se elige errar por ese lado a propósito: un "no lo sé" es recuperable
—la persona reformula— mientras que una respuesta inventada que suena convincente
no lo es.

Con un corpus más grande el solape tiende a reducirse, porque hay más
probabilidad de que exista un fragmento realmente cercano a cada pregunta. Si
necesitas más precisión que la que da un umbral, el siguiente paso es añadir un
*reranker* que reordene los candidatos con un modelo cruzado; fastembed incluye
alguno, a costa de más latencia.

## Ajustes

| Variable | Efecto |
|---|---|
| `RAG_TOP_K` | Cuántos fragmentos se recuperan. Más contexto, pero también más tokens y más latencia. |
| `RAG_MAX_DISTANCE` | El umbral. Subirlo hace al agente más hablador y más propenso a inventar; bajarlo, más honesto y más "no lo sé". |
| `CHUNK_SIZE` | Tamaño de fragmento. Sube si tus documentos tienen ideas largas; baja si son muy densos. **Requiere reindexar.** |
| `CHUNK_OVERLAP` | Solape. **Requiere reindexar.** |
| `EMBEDDING_MODEL` | El modelo. **Requiere reindexar.** |
| `CHROMA_COLLECTION` | Prefijo de las colecciones. Cada tema es `<prefijo>__<tema>`. **Requiere reindexar**: las colecciones con el prefijo viejo quedan huérfanas. |

## Cuánto cuesta tener muchos temas

Cada colección abierta carga su propio índice HNSW, así que la pregunta razonable
es si repartir el corpus sale caro en una placa con 3.8 GB. Medido, con el pico
de memoria del proceso que abre el buscador y hace dos consultas:

| Temas | Fragmentos | Pico de memoria |
|---|---|---|
| 1 | 4 | 696 MB |
| 4 | 16 | 699 MB |
| 12 | 48 | 725 MB |

Unos 2,4 MB por tema. Casi todo lo que se ve ahí es el modelo de embeddings, que
se carga una sola vez y no depende del número de colecciones. Con dos docenas de
temas el coste sigue siendo despreciable frente a los 2 GB del agente.
