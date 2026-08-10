# voice-agent-panel

Panel web para configurar y controlar el agente de voz sin editar código ni
entrar por SSH: prompt del sistema, alma, ajustes, herramientas, servidores MCP,
hooks, y el arranque y parada del servicio.

## Cómo le llega la configuración al agente

No hay ninguna llamada entre ambos. Corren en **contenedores distintos** y se
comunican por ficheros en el volumen de datos que los dos montan:

```
  panel  --escribe-->  <DATA_DIR>/config/settings.json     campos de Settings
                       <DATA_DIR>/config/runtime.json      prompt, alma, tools, mcp, hooks
  agente --escribe-->  <DATA_DIR>/config/estado_arranque.json   lo que cargó de verdad
                       <DATA_DIR>/logs/agente.log               el log en vivo
```

Guardar en el panel **no** cambia nada por sí solo. El ciclo completo es
`guardar → exportar → reiniciar`, y el panel lo hace de una vez desde el botón
de desplegar. El reinicio va por D-Bus contra systemd, no por la API de Podman:
la unidad la genera Quadlet con `--rm` y un `ExecStopPost` que destruiría el
contenedor recién rearrancado.

## Ejecutar en local

```bash
make panel           # migra, siembra el usuario y sirve en 127.0.0.1:8080
make panel-export    # solo exporta la configuración a data/config/
```

Necesita un `.env.panel` con al menos `PANEL_SECRET_KEY`. Ver `.env.panel.example`.

## Lo que este paquete NO puede hacer

Vive en una imagen sin Pipecat, así que no puede calcular el esquema JSON de una
herramienta ni lanzar un servidor MCP de tipo stdio. Ambas cosas las publica el
agente en `estado_arranque.json` cuando arranca — lo que además resulta ser
mejor, porque enseña lo que el agente tiene **de verdad** y no lo que debería
tener según esta base de datos. Cuando las dos cosas no coinciden, que es justo
cuando uno abre el panel, esa diferencia es el diagnóstico.
