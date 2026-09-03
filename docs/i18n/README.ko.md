<!-- i18n-sync: v10 -->

# Pseudolife-MCP

[영어 원본 README](../../README.md)와 동기화됨 — synced: v10 (2026-09-04)

**Claude Code, Codex, 그리고 그 밖의 MCP 클라이언트를 위한 영구적인 장기 메모리.**

코딩 에이전트에게 세션을 넘나들며 유지되는 장기 메모리를 제공하는 MCP 서버입니다 —
컨텍스트 압축과 새로운 작업에도 살아남습니다. 코딩 에이전트가 지능이고, 이 서버는
디스크에 저장되는 그 메모리입니다.

제공하는 기능:

- **정직하게 잊어버리는 연상 메모리** — 밀집 벡터 검색과 어휘 검색을 결합한
  하이브리드 검색을 갖춘 평면적인 유사도 저장소 위에서 모순을 탐지하고
  대체(supersession)를 수행합니다: 수정된 내용은 기존 답변 옆에 쌓이는 대신
  그것을 대체합니다.
- **막연한 느낌이 아닌 정규화된 사실** — `entity.attribute` 슬롯마다 하나의
  *현재* 값만 유지합니다(다만 여러 값이 동시에 존재할 수 있는 슬롯은 멤버
  집합으로 유지합니다). 수정은 조용히 덮어쓰는 대신 이전 값을 대체(supersede)하며,
  전체 버전 이력은 그대로 보존됩니다.
- **드림(Dreams)** — 자리를 비운 사이, 추출기(extractor)가 메모리 스트림을
  정규화된 사실과 지식 그래프로 통합합니다.
- **스스로의 작업에서 얻는 교훈** — 성공, 막다른 시도, 그리고 사용자의 수정
  사항이 매 세션 시작 시 제시되는 해야 할 것/피해야 할 것 가이드로 축적됩니다.
- **생각의 흐름을 지켜보는 웹 콘솔** — Cortex Console: 메모리 스트림, 사실
  이력, 지식 그래프 아틀라스, 세션 에피소드, 문서 RAG를 제공합니다.

## 빠른 시작

명령 두 줄이면 충분합니다. Docker도, 따로 구성할 데이터베이스도, 컨테이너
런타임도 필요 없습니다:

```bash
pip install "pseudolife-mcp[lite]"
claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
```

Claude Code 대신 Codex를 쓴다면 — 형태는 동일합니다:

```bash
pip install "pseudolife-mcp[lite]"
codex mcp add pseudolife-memory -- pseudolife-mcp
```

이후 두 코딩 에이전트 중 어느 쪽에서든 *"내 스테이징 박스는 haze-02라고
기억해줘"*라고 말하면 — 며칠 후 새 세션에서 *"스테이징 박스가 뭐였지?"*라고
물었을 때 메모리에서 답을 가져옵니다. Cortex Console
(`http://127.0.0.1:8765/ui/`)에서 모든 내용을 둘러볼 수 있습니다.

첫 세션에서 데몬이 자동으로 시작되며, 내장형 PostgreSQL을 프로비저닝하고
임베딩 모델을 내려받습니다 — 이는 일회성 단계입니다. Lite 구성에는 드림
추출기(extractor)가 포함되어 있지 않아 정규화된 사실이 저절로 생성되지
않습니다: 이 경로에서는 OpenAI 호환 엔드포인트가 설정되기 전까지
`memory_fact_set`만이 유일한 코텍스(cortex) 기록자입니다.

### 영구 보존 티어 — Docker

장기간 운영되는 뱅크(bank)를 원한다면: 위 내용 전체에 더해 번들 추출기, 외부
볼륨, 상태 점검이 적용된 서비스, 백업/롤백 도구가 포함됩니다. Docker와
MCP를 지원하는 코딩 에이전트가 최소 하나 필요합니다 — Claude Code, Codex,
Gemini CLI는 엔드투엔드로 연결되어 있으며, 그 외의 에이전트에는 바로
붙여넣을 수 있는 설정을 제공합니다. 클론부터 첫 메모리 저장까지 명령
한 줄이면 충분합니다:

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

설치 스크립트는 필수 구성 요소를 점검하고(누락된 항목이 있으면 정확한 해결
명령을 한 줄로 출력합니다), 어떤 드림 추출기를 사용할지 묻습니다 — Max 플랜을
통한 Claude 모델(가장 가벼운 설치), 번들 로컬 모델을 자동 폴백으로 사용하는
Claude shim, ChatGPT 플랜에서 GPT-5.6 모델을 쓰는 같은 두 가지 구성(Codex
CLI 경유), 또는 플랜이 전혀 필요 없는 번들 로컬 모델 단독 중에서 고를 수
있습니다. 그런 다음 스택을 띄우고, 선택한 클라이언트를 연결하며(매 세션 메모리
루프 안내를 전달하는 세션 시작 브리핑 훅과 MCP 전송 등록), 데몬 상태를 점검합니다.
멱등적(idempotent)으로 동작하므로 언제든 다시 실행해도 안전하며,
`--extractor <mode>`로 추출기 설정을 전환할 수 있습니다.

데몬이 실행 중이라면, Claude Code **플러그인**은 세션 시작 시 메모리 브리핑,
상시 메모리 루프 안내, 그리고 `/dream` + `/memory-status` 명령을 추가합니다 —
MCP 서버 자체는 설치 스크립트가 등록하므로, 플러그인이 도구를 이중으로 등록하는
일은 없습니다:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex — 설치 스크립트의 기본값(shim 모드)은 Claude에 사용하는 것과 동일한
stdio shim을 연결하며, Docker 티어에서 `PSEUDOLIFE_MCP_NO_SPAWN=1`을 설정해
Codex 세션이 동시에 실행 중인 Claude 세션의 에피소드를 물려받지 않고 자신만의
정체성(identity)을 갖도록 합니다. 정확한 명령어, 직접 HTTP로 연결하는 대안,
기본값이 아닌 포트/토큰 설정은
[README — 코딩 에이전트에 연결하기](../../README.md#wire-into-your-coding-agent)
문서를 참고하세요.

## 동작 방식

에이전트는 작업하면서 한 번에 하나씩 주장(claim)을 저장합니다(`memory_store`,
`memory_fact_set`). 세션 사이에는 **드림(dream)**이 스트림을 정규화된
사실, 그래프 관계, 절차적 교훈으로 압축합니다. 매 세션 시작 시 브리핑이 메모리가 확신하지
못하는 부분, 과거 작업에서 얻은 교훈, 그리고 지난번에 멈춘 지점을 주입합니다.
검색(retrieval)은 연상 저장소에 대한 의미 기반 검색과 정규화된 사실 저장소를
결합하므로, 수정된 답변이 오래된 답변을 이깁니다.

## 문서 (영어)

정본이자 항상 최신 상태로 유지되는 문서는 영어로 제공됩니다:

- [README](../../README.md) — 전체 설치, 연결 방법, 도구, 문제 해결
- [설정](../guide/configuration.md) · [검색](../guide/retrieval.md)
  · [드리밍](../guide/dreaming.md) · [에피소드](../guide/episodes.md)
  · [메모리 모델](../guide/memory-model.md) · [벤치마크](../guide/benchmarks.md)

이 페이지는 영어 README를 번역한 소개 문서로, 아래에 명시된 버전을 기준으로
동기화되어 있습니다. 내용이 서로 다를 경우 영어 문서가 기준입니다.
