# voice-agent-core

Configuración y modelos de datos compartidos entre el **agente de voz**
(`voice_agent`) y su **panel de control** (`voice_agent_panel`).

Existe por una razón muy concreta: el panel necesita la clase `Settings` para
generar sus formularios por introspección y para validar lo que exporta, pero
no puede depender del paquete del agente sin arrastrar Pipecat, chromadb y
fastembed —1,1 GB— a una imagen que debe ser pequeña y rápida de reconstruir.

Por eso aquí solo hay:

| Módulo | Contenido |
|---|---|
| `config.py` | `Settings`, la fuente `PanelSettingsSource` y `CAMPOS_PROTEGIDOS` |
| `runtime.py` | `RuntimeConfig`: prompt, alma, herramientas, servidores MCP y hooks |
| `prompts.py` | Los textos por defecto: `PROMPT_SISTEMA`, `SALUDO_INICIAL`, `MULETILLAS` |
| `estado.py` | `EstadoArranque`, el canal de vuelta del agente hacia el panel |
| `board.py` | Lectura de temperatura, memoria, carga y tiempo encendido |
| `rutas.py` | Rutas dentro de `DATA_DIR` y escritura atómica de JSON |

**Regla de oro: nada de este paquete puede importar `pipecat`, `chromadb` ni
`fastembed`.** No es una convención, es un test (`tests/test_core_liviano.py`).
