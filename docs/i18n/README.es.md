<!-- i18n-sync: v10 -->

# Pseudolife-MCP

> Traducción del [README](../../README.md) canónico — sincronizado: v10 (2026-09-04)

**Memoria persistente a largo plazo para Claude Code, Codex y otros clientes MCP.**

Un servidor MCP que le da a los agentes de codificación una memoria de largo
plazo que persiste entre sesiones — sobreviviendo a las compactaciones de
contexto y a las tareas nuevas. Tu agente de codificación es la
inteligencia; este servidor es su memoria en disco.

Lo que obtienes:

- **Memoria asociativa con un olvido honesto** — un almacén plano de
  similitud con recuperación híbrida densa y léxica, detección de
  contradicciones y sustitución: las correcciones reemplazan las
  respuestas antiguas en lugar de acumularse junto a ellas.
- **Hechos canónicos, no intuiciones** — un único valor *actual* por cada
  slot `entity.attribute` (o un conjunto de miembros, para los slots que
  contienen muchos valores simultáneos); las correcciones sustituyen en
  lugar de sobrescribir en silencio, y se conserva el historial completo de
  versiones.
- **Sueños** — mientras estás fuera, un extractor consolida el flujo de
  memoria en hechos canónicos y un grafo de conocimiento.
- **Lecciones de su propio trabajo** — los aciertos, los callejones sin
  salida y tus correcciones se convierten en pautas de qué hacer y qué
  evitar, que aparecen al inicio de cada sesión.
- **Una consola web para observar cómo piensa** — la Cortex Console: flujo
  de memoria, historial de hechos, atlas del grafo de conocimiento,
  episodios de sesión y RAG de documentos.

## Inicio rápido

Dos comandos. Sin Docker, sin base de datos que configurar, sin runtime de
contenedores:

```bash
pip install "pseudolife-mcp[lite]"
claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
```

Codex en lugar de Claude Code — misma forma:

```bash
pip install "pseudolife-mcp[lite]"
codex mcp add pseudolife-memory -- pseudolife-mcp
```

Luego, en cualquiera de los dos agentes de codificación: *"recuerda que mi
servidor de staging es haze-02"* — y en una sesión nueva, días después,
*"¿cuál es el servidor de staging?"* obtiene la respuesta de vuelta desde
la memoria. Explora todo en la Cortex Console en
`http://127.0.0.1:8765/ui/`.

La primera sesión inicia automáticamente el daemon, que aprovisiona un
PostgreSQL embebido y descarga el modelo de embeddings — un paso único.
Lite no incluye ningún **extractor** de sueños, así que los hechos
canónicos no aparecen por sí solos: en esta vía, `memory_fact_set` es el
único escritor del **cortex**, hasta que se configure un endpoint
compatible con OpenAI.

### Nivel duradero — Docker

Para un banco duradero: todo lo anterior, más el extractor incluido,
volúmenes externos, servicios con verificación de salud, y herramientas de
respaldo y reversión. Requiere Docker y al menos un agente de codificación
compatible con MCP — Claude Code, Codex y Gemini CLI están integrados de
extremo a extremo; cualquier otro recibe una configuración lista para
pegar. Un solo comando desde el clone hasta el primer recuerdo:

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
# Codex: add --client codex / -Client codex
# Both:  add --client both  / -Client both
# Gemini: add --client gemini — or several: --client claude,codex,gemini
# Other MCP agents (Cursor, Windsurf, Zed, ...): --client generic
```

El instalador comprueba los requisitos previos (mostrando una línea exacta
de solución para lo que falte) y pregunta qué extractor de sueños usar —
un modelo Claude a través de tu plan Max (la instalación más ligera), el
shim de Claude con el modelo local incluido como respaldo automático, las
mismas dos variantes con un modelo GPT-5.6 en un plan ChatGPT (vía Codex
CLI), o el modelo local incluido por sí solo, que no necesita ningún plan. Luego
levanta la pila, conecta los clientes seleccionados (el hook de resumen al
inicio de sesión, que entrega la guía del ciclo de memoria en cada sesión,
y el registro del transporte MCP), y verifica el estado del daemon. Es
idempotente: puedes volver a ejecutarlo en cualquier momento;
`--extractor <mode>` cambia la configuración del extractor.

Con el daemon en ejecución, el **plugin** de Claude Code añade el resumen
de memoria al inicio de sesión, la guía permanente del ciclo de memoria y
los comandos `/dream` + `/memory-status` — el propio servidor MCP lo
registra el instalador, así que el plugin nunca duplica sus herramientas:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex — la opción por defecto del instalador (modo shim) conecta el mismo
shim stdio que usa para Claude, manteniendo `PSEUDOLIFE_MCP_NO_SPAWN=1`
activo en el nivel Docker para que una sesión de Codex tenga su propia
identidad en lugar de heredar el episodio de una sesión de Claude
concurrente. Los comandos exactos, la alternativa de HTTP directo y los
puertos/tokens no predeterminados:
[README — Conectar tu agente de codificación](../../README.md#wire-into-your-coding-agent).

## Cómo funciona

El agente guarda una afirmación a la vez mientras trabaja (`memory_store`,
`memory_fact_set`).
Entre sesiones, el **sueño** destila el flujo en hechos canónicos,
relaciones de grafo y lecciones de procedimiento. Al inicio de cada
sesión, un resumen inyecta aquello de lo que la memoria no está segura,
las lecciones del trabajo anterior y dónde quedaste. La recuperación
combina la búsqueda semántica sobre el almacén asociativo con el almacén
de hechos canónicos, de modo que las respuestas corregidas prevalecen
sobre las obsoletas.

## Documentación (en inglés)

La documentación canónica y siempre actualizada está en inglés:

- [README](../../README.md) — instalación completa, integración,
  herramientas, solución de problemas
- [Configuración](../guide/configuration.md) · [Recuperación](../guide/retrieval.md)
  · [Sueños](../guide/dreaming.md) · [Episodios](../guide/episodes.md)
  · [Modelo de memoria](../guide/memory-model.md) · [Puntos de referencia](../guide/benchmarks.md)

Esta página es una introducción traducida, sincronizada con el README en
inglés en la versión indicada más arriba; donde difieran, la
documentación en inglés es la referencia.
