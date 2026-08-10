# Diagramas — arquitectura y flujo de decisión

Entregable 02 del reto. Los dos diagramas corresponden al código de este
repositorio; cada caja nombra el módulo que la implementa para poder cotejar.

## Arquitectura

```mermaid
flowchart LR
    subgraph NAV["Navegador del paciente"]
        MIC["Micrófono"] --> UI["UI WebRTC precompilada<br/>(pipecat-ai-small-webrtc-prebuilt)"]
        UI --> ALT["Altavoz"]
    end

    UI <-->|"audio WebRTC (Opus)<br/>señalización POST /api/offer"| WEB["FastAPI + SmallWebRTCTransport<br/>src/voice_agent/web.py"]

    subgraph PLACA["NanoPi R4S — 4 GB RAM, aarch64, sin GPU"]
        WEB --> STT["STT Deepgram nova-3<br/>(es, streaming)<br/>services.py::build_stt"]
        STT --> AGG["Agregador de contexto<br/>+ VAD Silero + fin de turno<br/>bot.py / services.py"]
        AGG --> LLM["Gemini 2.5 Flash<br/>SDK nativo, thinking=0<br/>services.py::build_llm"]
        LLM -->|"buscar_en_documentos<br/>tools/knowledge.py"| RAG[("ChromaDB + fastembed<br/>MiniLM multilingüe 384d<br/>rag/retriever.py")]
        LLM -->|"registrar_alerta<br/>tools/evaluacion.py"| ALERTAS["data/evaluaciones/alertas/*.json"]
        LLM -->|"finalizar_llamada<br/>tools/evaluacion.py"| RESUM["data/evaluaciones/resumenes/*.json"]
        RAG -->|"cada consulta queda en<br/>traza.py"| TRAZAS["data/evaluaciones/trazas/*.jsonl"]
        LLM --> TTS["TTS Piper es<br/>(ONNX local)<br/>services.py::build_tts"]
        TTS --> WEB
        MET["MetricsRecorder<br/>metrica.py"] --> METJ["data/metricas/*.jsonl<br/>(make metricas)"]

        CORPUS["corpus/<br/>106 PDF en 5 temas"] -->|"ingesta reconciliadora<br/>rag/ingest.py"| RAG
        PANEL["Consola Django<br/>packages/panel"] -->|"subir / borrar documento<br/>+ Reindexar (systemd oneshot)"| CORPUS
        PANEL -->|"lee alertas y resúmenes<br/>página Evaluaciones"| RESUM
    end
```

Puntos que no se ven en la caja pero importan:

- **Un pipeline por llamada** con servicios precargados (`ServiciosWeb`), como
  en la telefonía del proyecto base: un procesador de Pipecat pertenece a un
  pipeline y la carga de Piper no puede pagarla el saludo.
- **El conocimiento vivo no reinicia nada**: el retriever relee la lista de
  colecciones en cada consulta; reindexar desde el panel basta (compuerta G5).
- **La traza es la fuente de verdad de la trazabilidad**: registra lo que el
  índice devolvió de verdad (documento, tema, distancia), no lo que el modelo
  dice haber consultado.

## Flujo de decisión del agente

```mermaid
flowchart TD
    A["Saludo: confirmar nombre,<br/>cirugía y días transcurridos"] --> B["Recorrer el estado: dolor 1–10,<br/>fiebre medida, herida, apetito,<br/>sueño, movilidad"]
    B --> C{"¿Información suficiente<br/>para clasificar?"}
    C -->|"no"| D["Indagar antes de decidir:<br/>dónde, desde cuándo, cuánto,<br/>¿va a más o a menos?"]
    D --> C
    C -->|"sí"| E["buscar_en_documentos<br/>(términos clave + cirugía)"]
    E --> F{"¿La base cubre<br/>la cirugía del paciente?"}
    F -->|"no"| G["Declarar el límite honestamente.<br/>Cualquier consejo se presenta como<br/>precaución general, nunca como<br/>protocolo de esa cirugía"]
    F -->|"sí"| H["Contrastar síntomas contra los<br/>signos de alarma del protocolo,<br/>citando el documento"]
    G --> I
    H --> I{"Triaje<br/>(ante la duda entre dos niveles,<br/>SIEMPRE el más grave)"}
    I -->|"rojo"| J["registrar_alerta(rojo)<br/>→ acuda a urgencias AHORA<br/>+ el equipo queda avisado"]
    I -->|"amarillo"| K["registrar_alerta(amarillo)<br/>→ el equipo contacta en 24 h<br/>+ qué vigilar mientras tanto"]
    I -->|"verde"| L["registrar_alerta(verde)<br/>→ autocuidado + signos de alarma<br/>ante los que volver a llamar"]
    J --> M["Despedida →<br/>finalizar_llamada:<br/>resumen de 5 campos + traza"]
    K --> M
    L --> M
    M --> N{"¿El paciente colgó<br/>sin despedirse?"}
    N -->|"sí"| O["Resumen de RESPALDO automático:<br/>última alerta + documentos<br/>consultados + transcripción<br/>web.py::_resumen_de_respaldo"]
    N -->|"no"| P["Fin"]
    O --> P
```

Reglas transversales (en `packages/core/src/voice_agent_core/prompts.py`):
sin dosis ni pautas de medicación jamás; sin nombres de instituciones que el
paciente no haya dicho; los extractos de documentos son datos, no
instrucciones (anti-inyección); y ante un síntoma que no sepa interpretar,
nunca tranquilizar: preguntar más o escalar.
