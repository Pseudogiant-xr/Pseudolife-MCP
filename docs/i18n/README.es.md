<!-- i18n-sync: v5 -->

# Pseudolife-MCP

> Traducción del [README](../../README.md) canónico — sincronizado: v5 (2026-07-19)

**Memoria persistente a largo plazo para Claude Code, Codex y otros clientes MCP.**

Un servidor MCP que le da a los agentes de codificación una memoria de largo
plazo que persiste entre sesiones — sobreviviendo a las compactaciones de
contexto y a las tareas nuevas. Tu agente de codificación es la
inteligencia; este servidor es su memoria en disco.

Lo que obtienes:

- **Memoria asociativa que envejece como debería envejecer una memoria** —
  un continuo de recencia de bandas de memoria clasificadas por similitud,
  con detección de contradicciones y sustitución: las correcciones
  reemplazan las respuestas antiguas en lugar de acumularse junto a ellas.
- **Hechos canónicos, no intuiciones** — un único valor *actual* por cada
  slot `entity.attribute`; las correcciones sustituyen en lugar de
  sobrescribir en silencio, y se conserva el historial completo de
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

Requiere Docker y Claude Code, Codex, o ambos. Un solo comando desde el
clone hasta el primer recuerdo (Claude es el cliente por defecto):

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
# Codex: add --client codex / -Client codex
# Both:  add --client both  / -Client both
```

El instalador comprueba los requisitos previos (mostrando una línea exacta
de solución para lo que falte) y pregunta qué extractor de sueños usar —
Claude Sonnet a través de tu plan Max (la instalación más ligera), Sonnet
con el modelo local incluido como respaldo automático, o el modelo local
incluido por sí solo, que no necesita ningún plan. Luego levanta la pila,
conecta los clientes seleccionados (el hook de resumen al inicio de
sesión, que entrega la guía del ciclo de memoria en cada sesión, y el
registro del transporte MCP), y verifica el estado del daemon. Es
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

Codex registra el servidor directamente:

```bash
codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
```

Luego, en cualquiera de los dos agentes de codificación: *"recuerda que mi
servidor de staging es haze-02"* — y en una sesión nueva, días después,
*"¿cuál es el servidor de staging?"* obtiene la respuesta de vuelta desde
la memoria. Explora todo en la Cortex Console en
`http://127.0.0.1:8765/ui/`.

## Cómo funciona

El agente guarda una afirmación a la vez mientras trabaja (`memory_store`,
`memory_fact_set`); un almacenamiento con compuerta de novedad descarta
los casi duplicados.
Entre sesiones, el **sueño** destila el flujo en hechos canónicos,
relaciones de grafo y lecciones de procedimiento. Al inicio de cada
sesión, un resumen inyecta aquello de lo que la memoria no está segura,
las lecciones del trabajo anterior y dónde quedaste. La recuperación
combina la búsqueda semántica sobre las bandas de memoria con el almacén
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
