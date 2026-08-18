# Informe final — Clara, agente de voz de seguimiento postoperatorio

Informe técnico del proyecto, agosto de 2026. **[BORRADOR — revisar antes de
publicar.]**

## 1. Qué se construyó

Una asistente virtual de enfermería ("Clara") que atiende llamadas de voz por
navegador en tiempo real, conversa en español con pacientes colombianos
recién operados, responde apoyándose en un corpus clínico indexado con
conocimiento vivo, clasifica cada llamada en un triaje verde/amarillo/rojo,
persiste una alerta estructurada al decidir escalar y deja un resumen
trazable de cada llamada. Todo corre en una NanoPi R4S (4 GB de RAM, ARM,
sin GPU) expuesta por un túnel de Cloudflare; el coste de modelo por llamada
es $0 en niveles gratuitos.

## 2. El modelo de lenguaje

**Modelo usado: `gemini-2.5-flash` (familia Google Gemini, gama Flash), en el
nivel gratuito de Google AI Studio, mediante el SDK nativo `google-genai`.**

Por qué este y no otro:

- **Latencia del primer token.** En un agente de voz, el tiempo hasta el
  primer token ES la conversación. Medido desde la placa: mediana de 0,63 s
  hasta el primer token, con el razonamiento desactivado
  (`thinking_budget=0`) porque el "pensamiento" de Gemini 2.5 se cobra en
  segundos de silencio.
- **Function calling sólido.** El agente vive de sus herramientas (RAG,
  alerta, resumen); Gemini las invoca con fiabilidad y con argumentos bien
  formados en español.
- **Plan B declarado y conmutable.** `LLM_BACKEND=groq` cambia a Llama 3.3
  70B servido por Groq (también en nivel gratuito) sin tocar código,
  por si el límite de peticiones por minuto del nivel gratuito de Gemini se
  quedara corto en una sesión en vivo.
- **Decisión técnica relevante durante el desarrollo**: empezamos usando el
  endpoint *OpenAI-compatible* de AI Studio (cero dependencias nuevas) y una
  llamada real lo descartó: su streaming emite tool-calls fantasma con nombre
  vacío que envenenan el historial y Gemini rechaza después TODA petición
  (error 400), dejando muda a la asistente a mitad de llamada. El SDK nativo
  no pasa por ese shim. El commit `4794d0f` documenta la depuración.

## 3. Arquitectura y decisiones

Diagramas en [`docs/diagramas.md`](diagramas.md). Stack: Pipecat 1.6
(orquestación), WebRTC vía aiortc con UI precompilada, Deepgram nova-3
para STT en español (0 % WER medido en pruebas propias contra 33 % del
Whisper local viable en esta placa), Piper para TTS local, ChromaDB embebido
+ fastembed (embeddings multilingües, porque el corpus mezcla español e
inglés y las preguntas llegan en español), consola Django para el
conocimiento vivo.

Decisiones con su porqué, todas verificables en el código:

1. **RAG por temas con umbral anti-alucinación calibrado con sondas** (tabla
   completa en `docs/rag.md`): cubiertas ≤ 0,41 de distancia, ajenas ≥ 0,56;
   umbral 0,52. El umbral heredado (0,68) dejaba pasar documentos de otra
   cirugía ante preguntas fuera de corpus.
2. **Anclaje honesto por tema**: la herramienta de búsqueda le dice al modelo
   qué cirugías cubre la base y le prohíbe atribuir extractos a guías de una
   cirugía ausente. Salió de una prueba real (paciente de cataratas — cirugía
   fuera del corpus — al que el agente le citó "las guías de cataratas"
   apoyándose en documentos de colecistitis).
3. **La alerta se persiste al decidir, no al colgar** (asimetría clínica: el
   falso negativo es la falla catastrófica; una llamada que se cae tras
   detectar una bandera roja tiene que dejar la alerta ya en disco).
4. **Resumen de respaldo**: si el paciente cuelga sin despedirse —que pasa a
   menudo—, el pipeline persiste un resumen con la
   última alerta, los documentos consultados y la transcripción completa.
5. **Trazabilidad que resiste verificación**: cada consulta al RAG queda en
   un JSONL por llamada con lo que el índice devolvió de verdad; el campo
   `documentos_consultados` del resumen sale de esa traza, no de la memoria
   del modelo.
6. **Reindexado utilizable en vivo**: la reconciliación no re-embebe
   fragmentos sin cambios (ids con hash del contenido): reindexar el corpus
   pasó de ~57 min a ~4,5 min medidos, y añadir un documento cuesta lo que
   cuesta ese documento.

## 4. Métricas medidas

Ver la tabla del README (sección "Métricas medidas"), generada con
`make metricas` sobre los JSONL que el agente escribe en cada llamada real:
**P50 1,65 s / P95 2,78 s** de voz a voz, 2,7 invocaciones de modelo por
turno, ≈ $0,0065 por llamada extrapolado a precios de pago (real: $0,00).

## 5. Proceso de trabajo con IA

El desarrollo se hizo en pareja con un agente de IA (Claude Code) sobre una
base propia previa (un agente de voz doméstico para la misma placa, del mismo
autor). El historial de commits del repositorio es el registro fiel del
proceso: cada commit explica qué se cambió y por qué, incluidas las
depuraciones con llamadas reales que tumbaron dos hipótesis (el endpoint
OpenAI-compatible, el umbral RAG heredado) y los defectos que las pruebas
adversariales propias destaparon antes de que llegaran a un paciente
(atribución a guías
inexistentes, invención del nombre de una clínica, sesiones zombi al recargar
la pestaña, resumen perdido al colgar sin despedida).

La evaluación de prompts fue empírica: cada ajuste se probó con llamadas
reales por el navegador contra la placa, observando el log del pipeline (qué
consultó, qué recuperó, qué persistió) y los ficheros de evaluación.

## 6. Seguridad y resistencia a inyección

- Reglas inmutables en el prompt (no revelar instrucciones, no cambiar de
  rol, no aceptar órdenes del interlocutor ni de los documentos).
- Los extractos del RAG van blindados como datos: un PDF subido con órdenes
  dirigidas al modelo se trata como texto citado.
- Prohibición absoluta de dosis y pautas de medicación, incluso si aparecen
  en un documento.
- La consola exige autenticación —con freno de fuerza bruta por IP— y la
  interfaz de voz, un código de acceso que viaja en el enlace y se canjea por
  una galleta firmada. Los hooks de comandos están desactivados en el
  despliegue expuesto; las claves de API viven solo en el `.env` de la placa.
- La superficie expuesta tiene límites propios: una conversación a la vez, con
  tope de duración e inactividad y cuota de llamadas por IP. Una llamada nueva
  no desaloja a quien está hablando. Ver `docs/seguridad.md`.

## 7. Limitaciones conocidas

Las del README (sección homónima): reindexado ~5 min, un PDF escaneado sin
capa de texto excluido, límites del nivel gratuito con plan B documentado,
voz es-ES (no hay voz colombiana comparable en Piper).

## 8. Notas de presentación

**El valor.** El seguimiento postoperatorio hoy depende de
llamadas humanas caras que no escalan, y la ventana de las primeras 72 horas
es donde una complicación detectada a tiempo cambia el desenlace. Clara hace
la llamada, entiende al paciente en su propio lenguaje, se apoya en los
protocolos vigentes del hospital —que el personal actualiza subiendo un PDF,
sin ingenieros—, decide con criterio clínico conservador y deja un registro
verificable de cada decisión. El diferencial frente a un chatbot: es voz en
tiempo real con latencia de conversación, cada afirmación clínica es
rastreable hasta el documento que la sustenta, y el sistema está diseñado
para la asimetría clínica (ante la duda, escala). Y corre entera en un
ordenador de 60 dólares con coste de modelo cero.

**La decisión técnica más relevante.** Cómo anclar las
respuestas clínicas. Evaluamos (a) confiar en las citas del modelo
—descartado: en una prueba real citó "las guías de cataratas" que no
existían—; (b) subir el umbral de similitud —descartado: las sondas midieron
que el vocabulario clínico genérico cuela documentos de otra cirugía—; y (c)
lo implementado: umbral calibrado con sondas + traza real de cada consulta +
la herramienta declara qué cirugías cubre la base y obliga a decir "mi base
no cubre su cirugía" en vez de disfrazar la prudencia de cita. Riesgo
identificado: el falso "no lo sé" en preguntas legítimas; se mitigó con
consultas por términos clave y top-k 5. Con dos semanas más: OCR del PDF
escaneado, reranker ligero para el cruce español↔inglés, resumen de respaldo
redactado por el modelo (hoy es factual), y una voz colombiana entrenada para
Piper.
