<!-- i18n-sync: v10 -->

# Pseudolife-MCP

[英文版 README](../../README.md) · 已同步:v10 (2026-09-04)

**为 Claude Code、Codex 及其他 MCP 客户端提供持久的长期记忆。**

这是一个 MCP 服务器,为编码智能体提供跨会话持久保存的长期记忆——即使经历上下文压缩和全新任务,记忆依然留存。你的编码智能体负责智能本身,这个服务器则是它落在磁盘上的记忆。

你将获得:

- **诚实遗忘的联想记忆** —— 一个扁平的相似度存储,结合密集向量检索与词法检索的混合方式,内置矛盾检测与替代机制:更正会取代旧答案,而不是在其旁边不断堆积。
- **规范事实,而非模糊印象** —— 每个 `entity.attribute` 槽位只保留一个*当前*值(对于可同时持有多个并发值的槽位,则保留一个成员集合);更正会正式取代旧值,而不是被静默覆盖,完整的版本历史始终保留。
- **梦境整理** —— 在你离开期间,提取器会将记忆流整理为规范事实与知识图谱。
- **从自身工作中提炼的经验教训** —— 成功、走过的弯路,以及你的更正,都会转化为「应做/应避免」的指导,在每次会话开始时呈现。
- **一个可以看到它「思考」过程的网页控制台** —— Cortex Console:记忆流、事实历史、知识图谱图集、会话片段与文档 RAG。

## 快速开始

只需两条命令。无需 Docker,无需搭建数据库,也无需容器运行时:

```bash
pip install "pseudolife-mcp[lite]"
claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
```

如果使用 Codex 而非 Claude Code——步骤相同:

```bash
pip install "pseudolife-mcp[lite]"
codex mcp add pseudolife-memory -- pseudolife-mcp
```

之后,在任意一种编码智能体中说一句:*“记住我的 staging 服务器是 haze-02”*——几天后开启一个全新会话,再问一句:*“哪台是 staging 服务器?”*,答案就会从记忆中被找回。你可以在 Cortex Console(`http://127.0.0.1:8765/ui/`)中浏览一切。

第一次会话会自动启动守护进程,由它配置一个内嵌的 PostgreSQL 并下载嵌入模型——这是一次性步骤。Lite 版本不附带梦境**提取器**,因此规范事实不会自动出现:在这条路径下,`memory_fact_set` 是唯一的**cortex**写入方式,直到配置了兼容 OpenAI 的接口端点为止。

### 持久层——Docker

若需要长期存续的记忆库:在上述一切的基础上,还包括内置提取器、外部卷、带健康检查的服务,以及备份/回滚工具。需要 Docker,以及至少一个支持 MCP 的编码智能体——Claude Code、Codex 与 Gemini CLI 已完成端到端接入;其他智能体则可获得可直接粘贴使用的配置。从克隆仓库到获得第一条记忆,只需一条命令:

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

安装脚本会检查前置依赖(缺少什么就打印一行明确的修复命令),并询问使用哪种梦境提取器——通过你的 Max 套餐调用某个 Claude 模型(安装最轻量)、使用 Claude shim 并以内置本地模型作为自动回退、在 ChatGPT 套餐上以同样的两种方式使用 GPT-5.6 模型(通过 Codex CLI),或者单独使用内置的本地模型(完全不需要任何套餐)。随后它会启动整套服务,为所选客户端完成接入(会话开始时的简报钩子——它会在每次会话中传递记忆循环指导——以及 MCP 传输注册),并对守护进程做健康检查。该脚本是幂等的:随时可以重复执行;`--extractor <mode>` 可用于切换提取器配置。

守护进程启动后,Claude Code 的**插件**会添加会话开始时的记忆简报、常驻记忆循环指导,以及 `/dream` 与 `/memory-status` 命令——MCP 服务器本身由安装脚本注册,因此插件绝不会重复注册它的工具:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex——安装脚本默认(shim 模式)会为其接入与 Claude 相同的 stdio shim,并在 Docker 层保持设置 `PSEUDOLIFE_MCP_NO_SPAWN=1`,使 Codex 会话拥有独立身份,而不会继承并发 Claude 会话的会话片段。具体命令、直接 HTTP 接入的替代方案,以及非默认端口/令牌配置,参见:[README——接入你的编码智能体](../../README.md#wire-into-your-coding-agent)。

## 工作原理

该智能体在工作过程中会逐条存入声明(`memory_store`、`memory_fact_set`)。在会话之间,**dream** 会把记忆流蒸馏为规范事实、图谱关系与过程性经验教训。每次会话开始时,简报都会注入记忆中尚不确定的部分、过往工作的经验教训,以及你上次停下的地方。检索会将联想存储上的语义搜索与规范事实库结合起来,使已更正的答案胜过过时的答案。

## 文档(英文)

权威且始终保持最新的文档使用英文撰写:

- [README](../../README.md) —— 完整的安装、接入、工具与故障排查说明
- [配置](../guide/configuration.md) · [检索](../guide/retrieval.md)
  · [梦境机制](../guide/dreaming.md) · [会话片段](../guide/episodes.md)
  · [记忆模型](../guide/memory-model.md) · [性能基准](../guide/benchmarks.md)

本页是面向中文读者的翻译版引言,已同步至下方标注版本的英文 README;如两者内容存在出入,以英文文档为准——英文文档是权威版本。
