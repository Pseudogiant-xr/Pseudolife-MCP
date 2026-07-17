<!-- i18n-sync: v1 -->

# Pseudolife-MCP

[영어 원본 README](../../README.md)와 동기화됨 — synced: v1 (2026-07-17)

**Model Context Protocol(MCP)을 통해 Claude Code에 영구적인 장기 메모리를 제공합니다.**

Claude(또는 MCP를 지원하는 모든 클라이언트)에게 세션을 넘나들며 유지되는 장기
메모리를 제공하는 MCP 서버입니다 — 컨텍스트 압축과 `/clear` 초기화에도 살아남습니다.
Claude는 LLM이고, 이 서버는 디스크에 저장되는 그 메모리입니다.

제공하는 기능:

- **기억처럼 자연스럽게 나이 드는 연상 메모리** — 유사도로 정렬된 메모리 밴드들이
  이루는 최신성의 연속체 위에서 모순을 탐지하고 대체(supersession)를 수행합니다:
  수정된 내용은 기존 답변 옆에 쌓이는 대신 그것을 대체합니다.
- **막연한 느낌이 아닌 정규화된 사실** — `entity.attribute` 슬롯마다 하나의
  *현재* 값만 유지합니다. 수정은 조용히 덮어쓰는 대신 이전 값을 대체(supersede)하며,
  전체 버전 이력은 그대로 보존됩니다.
- **드림(Dreams)** — 자리를 비운 사이, 추출기(extractor)가 메모리 스트림을
  정규화된 사실과 지식 그래프로 통합합니다.
- **스스로의 작업에서 얻는 교훈** — 성공, 막다른 시도, 그리고 사용자의 수정
  사항이 매 세션 시작 시 제시되는 해야 할 것/피해야 할 것 가이드로 축적됩니다.
- **생각의 흐름을 지켜보는 웹 콘솔** — Cortex Console: 메모리 스트림, 사실
  이력, 지식 그래프 아틀라스, 세션 에피소드, 문서 RAG를 제공합니다.

## 빠른 시작

Docker와 Claude Code가 필요합니다. 클론부터 첫 메모리 저장까지 명령 한 줄이면
충분합니다:

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
```

설치 스크립트는 필수 구성 요소를 점검하고(누락된 항목이 있으면 정확한 해결
명령을 한 줄로 출력합니다), 어떤 드림 추출기를 사용할지 묻습니다 — Max 플랜을
통한 Claude Sonnet(가장 가벼운 설치) 또는 어떤 플랜 없이도 동작하는 번들 로컬
모델 중 하나입니다 — 그런 다음 스택을 띄우고, Claude Code에 모든 것을 연결하고,
데몬 상태를 점검합니다. 멱등적(idempotent)으로 동작하므로 언제든 다시 실행해도
안전합니다.

데몬이 실행 중이라면, Claude Code **플러그인**이 가장 쉬운 연결 방법입니다 —
명령 두 줄로 MCP 서버, 세션 시작 시 메모리 브리핑, 그리고 `/dream` +
`/memory-status` 명령까지 한 번에 설정됩니다:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

이후 어떤 Claude Code 세션에서든 *"내 스테이징 박스는 haze-02라고
기억해줘"*라고 말하면 — 며칠 후 새 세션에서 *"스테이징 박스가 뭐였지?"*라고
물었을 때 메모리에서 답을 가져옵니다. Cortex Console
(`http://127.0.0.1:8765/ui/`)에서 모든 내용을 둘러볼 수 있습니다.

## 동작 방식

Claude는 작업하면서 한 번에 하나씩 주장(claim)을 저장하며(`memory_store`,
`memory_fact_set`), 서프라이즈 게이트(surprise gate)가 거의 중복된 항목을
걸러냅니다. 세션 사이에는 **드림(dream)**이 스트림을 정규화된 사실, 그래프
관계, 절차적 교훈으로 압축합니다. 매 세션 시작 시 브리핑이 메모리가 확신하지
못하는 부분, 과거 작업에서 얻은 교훈, 그리고 지난번에 멈춘 지점을 주입합니다.
검색(retrieval)은 메모리 밴드에 대한 의미 기반 검색과 정규화된 사실 저장소를
결합하므로, 수정된 답변이 오래된 답변을 이깁니다.

## 문서 (영어)

정본이자 항상 최신 상태로 유지되는 문서는 영어로 제공됩니다:

- [README](../../README.md) — 전체 설치, 연결 방법, 도구, 문제 해결
- [설정](../guide/configuration.md) · [검색](../guide/retrieval.md)
  · [드리밍](../guide/dreaming.md) · [에피소드](../guide/episodes.md)
  · [메모리 모델](../guide/memory-model.md) · [벤치마크](../guide/benchmarks.md)

이 페이지는 영어 README를 번역한 소개 문서로, 아래에 명시된 버전을 기준으로
동기화되어 있습니다. 내용이 서로 다를 경우 영어 문서가 기준입니다.
