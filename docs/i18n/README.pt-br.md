<!-- i18n-sync: v1 -->

# Pseudolife-MCP

[README original em inglês](../../README.md) — sincronizado: v1 (2026-07-17)

**Memória de longo prazo persistente para Claude Code via o Model Context Protocol.**

Um servidor MCP que dá ao Claude (ou a qualquer cliente compatível com MCP)
uma memória de longo prazo que persiste entre sessões — sobrevivendo a
compactações de contexto e a resets de `/clear`. Claude é o LLM; este
servidor é a memória dele em disco.

O que você ganha:

- **Memória associativa que envelhece como a memória deveria** — um
  contínuo de recência de bandas de memória, ranqueadas por similaridade,
  com detecção de contradição e supersessão: correções substituem
  respostas antigas em vez de se acumularem ao lado delas.
- **Fatos canônicos, não achismos** — um valor *atual* por slot
  `entity.attribute`; correções fazem supersessão em vez de sobrescrever
  silenciosamente, e o histórico completo de versões é preservado.
- **Sonhos** — enquanto você está fora, um extrator consolida o fluxo de
  memória em fatos canônicos e em um grafo de conhecimento.
- **Lições do próprio trabalho** — sucessos, becos sem saída e suas
  correções viram orientações do tipo "faça"/"evite" exibidas no início
  de cada sessão.
- **Um console web para observar o raciocínio** — o Cortex Console: fluxo
  de memória, histórico de fatos, atlas do grafo de conhecimento,
  episódios de sessão e RAG de documentos.

## Início rápido

Requer Docker e Claude Code. Um único comando do clone até a primeira memória:

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
```

O instalador verifica os pré-requisitos (imprimindo uma linha exata de
correção para qualquer item ausente), pergunta qual extrator de sonhos
usar — Claude Sonnet via seu plano Max (a instalação mais leve) ou um
modelo local incluído que funciona sem nenhum plano — sobe a stack,
conecta tudo ao Claude Code e faz o health-check do daemon. Ele é
idempotente: pode ser executado novamente a qualquer momento.

Com o daemon em execução, o **plugin** do Claude Code é a forma mais
simples de conectar tudo — dois comandos configuram o servidor MCP, o
briefing de memória no início da sessão e os comandos `/dream` e
`/memory-status`:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Em seguida, em qualquer sessão do Claude Code: *"lembre que minha máquina
de staging é haze-02"* — e, dias depois, em uma sessão nova, *"qual é a
máquina de staging?"* recebe a resposta de volta, vinda da memória.
Navegue por tudo no Cortex Console em `http://127.0.0.1:8765/ui/`.

## Como funciona

Claude armazena uma afirmação de cada vez enquanto trabalha
(`memory_store`, `memory_fact_set`); um surprise gate descarta
quase-duplicatas. Entre sessões, o **sonho** destila o fluxo em fatos
canônicos, relações de grafo e lições procedurais. No início de cada
sessão, um briefing injeta o que a memória tem incerteza, lições de
trabalhos anteriores e onde você parou. A recuperação combina busca
semântica sobre as bandas de memória com o repositório de fatos
canônicos, de modo que respostas corrigidas prevalecem sobre as
desatualizadas.

## Documentação (inglês)

A documentação canônica e sempre atualizada está em inglês:

- [README](../../README.md) — instalação completa, integração,
  ferramentas, solução de problemas
- [Configuração](../guide/configuration.md) · [Recuperação](../guide/retrieval.md)
  · [Sonhos](../guide/dreaming.md) · [Episódios](../guide/episodes.md)
  · [Modelo de memória](../guide/memory-model.md) · [Benchmarks](../guide/benchmarks.md)

Esta página é uma introdução traduzida, sincronizada com o README em
inglês na versão indicada acima; em caso de divergência, a documentação
em inglês é a referência.
