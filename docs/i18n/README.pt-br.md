<!-- i18n-sync: v4 -->

# Pseudolife-MCP

[README original em inglês](../../README.md) — sincronizado: v3 (2026-07-19)

**Memória de longo prazo persistente para Claude Code, Codex e outros clientes MCP.**

Um servidor MCP que dá a agentes de codificação uma memória de longo prazo
que persiste entre sessões — sobrevivendo a compactações de contexto e a
novas tarefas. Seu agente de codificação é a inteligência; este servidor é
a memória dele em disco.

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

Requer Docker e Claude Code, Codex, ou ambos. Um único comando do clone
até a primeira memória (Claude é o cliente padrão):

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
# Codex: add --client codex / -Client codex
# Both:  add --client both  / -Client both
```

O instalador verifica os pré-requisitos (imprimindo uma linha exata de
correção para qualquer item ausente) e pergunta qual extrator de sonhos
usar — Claude Sonnet via seu plano Max (a instalação mais leve), Sonnet
com o modelo local incluído como fallback automático, ou apenas o modelo
local incluído, que não precisa de nenhum plano. Em seguida, ele sobe a
stack, conecta os clientes selecionados (o hook de briefing no início da
sessão e o registro do transporte MCP), oferece anexar a instrução
permanente do loop de memória a `~/.claude/CLAUDE.md` ou
`~/.codex/AGENTS.md`, e faz o health-check do daemon. Ele é idempotente:
pode ser executado novamente a qualquer momento; `--extractor <mode>`
alterna entre as configurações de extrator.

Com o daemon em execução, o **plugin** do Claude Code adiciona o briefing
de memória no início da sessão, a orientação permanente do loop de
memória e os comandos `/dream` e `/memory-status` — o próprio servidor
MCP é registrado pelo instalador, então o plugin nunca duplica as
ferramentas dele:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex registra o servidor diretamente:

```bash
codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
```

Em seguida, em qualquer um dos agentes de codificação: *"lembre que minha
máquina de staging é haze-02"* — e, dias depois, em uma sessão nova,
*"qual é a máquina de staging?"* recebe a resposta de volta, vinda da
memória. Navegue por tudo no Cortex Console em `http://127.0.0.1:8765/ui/`.

## Como funciona

O agente armazena uma afirmação de cada vez enquanto trabalha
(`memory_store`, `memory_fact_set`); um armazenamento com filtro de
novidade descarta quase-duplicatas. Entre sessões, o **sonho** destila o fluxo em fatos
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
