<!-- i18n-sync: v10 -->

# Pseudolife-MCP

[README original em inglês](../../README.md) — sincronizado: v10 (2026-09-04)

**Memória de longo prazo persistente para Claude Code, Codex e outros clientes MCP.**

Um servidor MCP que dá a agentes de codificação uma memória de longo prazo
que persiste entre sessões — sobrevivendo a compactações de contexto e a
novas tarefas. Seu agente de codificação é a inteligência; este servidor é
a memória dele em disco.

O que você ganha:

- **Memória associativa com esquecimento honesto** — um armazenamento
  plano por similaridade com recuperação híbrida densa e lexical,
  detecção de contradição e supersessão: correções substituem respostas
  antigas em vez de se acumularem ao lado delas.
- **Fatos canônicos, não achismos** — um valor *atual* por slot
  `entity.attribute` (ou um conjunto de membros, para slots que armazenam
  muitos valores simultâneos); correções fazem supersessão em vez de
  sobrescrever silenciosamente, e o histórico completo de versões é
  preservado.
- **Sonhos** — enquanto você está fora, um extrator consolida o fluxo de
  memória em fatos canônicos e em um grafo de conhecimento.
- **Lições do próprio trabalho** — sucessos, becos sem saída e suas
  correções viram orientações do tipo "faça"/"evite" exibidas no início
  de cada sessão.
- **Um console web para observar o raciocínio** — o Cortex Console: fluxo
  de memória, histórico de fatos, atlas do grafo de conhecimento,
  episódios de sessão e RAG de documentos.

## Início rápido

Dois comandos. Sem Docker, sem banco de dados para configurar, sem
runtime de container:

```bash
pip install "pseudolife-mcp[lite]"
claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
```

Codex em vez do Claude Code — mesmo formato:

```bash
pip install "pseudolife-mcp[lite]"
codex mcp add pseudolife-memory -- pseudolife-mcp
```

Em seguida, em qualquer um dos agentes de codificação: *"lembre que minha
máquina de staging é haze-02"* — e, em uma sessão nova dias depois,
*"qual é a máquina de staging?"* recebe a resposta de volta, vinda da
memória. Navegue por tudo no Cortex Console em `http://127.0.0.1:8765/ui/`.

A primeira sessão inicia o daemon automaticamente, que provisiona um
PostgreSQL embutido e baixa o modelo de embedding — um passo único. O
Lite não vem com um **extractor** de sonhos, então fatos canônicos não
aparecem sozinhos: nesse caminho, `memory_fact_set` é o único gravador do
**cortex**, até que um endpoint compatível com OpenAI seja configurado.

### Camada durável — Docker

Para um banco de memória duradouro: tudo o que foi dito acima, mais o
extrator incluído, volumes externos, serviços com health-check e
ferramentas de backup/rollback. Requer Docker e pelo menos um agente de
codificação compatível com MCP — Claude Code, Codex e Gemini CLI estão
integrados de ponta a ponta; qualquer outro recebe configuração pronta
para colar. Um único comando do clone até a primeira memória:

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

O instalador verifica os pré-requisitos (imprimindo uma linha exata de
correção para qualquer item ausente) e pergunta qual extrator de sonhos
usar — um modelo Claude via seu plano Max (a instalação mais leve), o
shim do Claude com o modelo local incluído como fallback automático, as
mesmas duas formas com um modelo GPT-5.6 em um plano ChatGPT (via Codex
CLI), ou apenas o modelo local incluído, que não precisa de nenhum plano.
Em seguida, ele sobe a stack, conecta os clientes selecionados (o hook de
briefing no início da sessão, que entrega a orientação do loop de memória
a cada sessão, e o registro do transporte MCP), e faz o health-check do
daemon. Ele é idempotente: pode ser executado novamente a qualquer
momento; `--extractor <mode>` alterna entre as configurações de extrator.

Com o daemon em execução, o **plugin** do Claude Code adiciona o briefing
de memória no início da sessão, a orientação permanente do loop de
memória e os comandos `/dream` e `/memory-status` — o próprio servidor
MCP é registrado pelo instalador, então o plugin nunca duplica as
ferramentas dele:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex — o padrão do instalador (modo shim) conecta o mesmo shim stdio
usado para o Claude, mantendo `PSEUDOLIFE_MCP_NO_SPAWN=1` definido na
camada Docker para que uma sessão do Codex tenha sua própria identidade
em vez de herdar o episódio de uma sessão concorrente do Claude. Comandos
exatos, a alternativa via HTTP direto, e portas/tokens não padrão:
[README — Wire into your coding agent](../../README.md#wire-into-your-coding-agent).

## Como funciona

O agente armazena uma afirmação de cada vez enquanto trabalha
(`memory_store`, `memory_fact_set`). Entre sessões, o **sonho** destila o
fluxo em fatos canônicos, relações de grafo e lições procedurais. No início de cada
sessão, um briefing injeta o que a memória tem incerteza, lições de
trabalhos anteriores e onde você parou. A recuperação combina busca
semântica sobre o armazenamento associativo com o repositório de fatos
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
