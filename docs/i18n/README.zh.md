<!-- i18n-sync: v5 -->

# Pseudolife-MCP

[英文版 README](../../README.md) · 已同步:v5 (2026-07-19)

**为 Claude Code、Codex 及其他 MCP 客户端提供持久的长期记忆。**

这是一个 MCP 服务器,为编码智能体提供跨会话持久保存的长期记忆——即使经历上下文压缩和全新任务,记忆依然留存。你的编码智能体负责智能本身,这个服务器则是它落在磁盘上的记忆。

你将获得:

- **像记忆本该有的方式一样自然衰减的联想记忆** —— 按相似度排序、构成一条近因连续谱的记忆带,内置矛盾检测与替代机制:更正会取代旧答案,而不是在其旁边不断堆积。
- **规范事实,而非模糊印象** —— 每个 `entity.attribute` 槽位只保留一个*当前*值;更正会正式取代旧值,而不是被静默覆盖,完整的版本历史始终保留。
- **梦境整理** —— 在你离开期间,提取器会将记忆流整理为规范事实与知识图谱。
- **从自身工作中提炼的经验教训** —— 成功、走过的弯路,以及你的更正,都会转化为「应做/应避免」的指导,在每次会话开始时呈现。
- **一个可以看到它「思考」过程的网页控制台** —— Cortex Console:记忆流、事实历史、知识图谱图集、会话片段与文档 RAG。

## 快速开始

需要 Docker,以及 Claude Code、Codex,或两者皆可。从克隆仓库到获得第一条记忆,只需一条命令(默认客户端为 Claude):

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
# Codex: add --client codex / -Client codex
# Both:  add --client both  / -Client both
```

安装脚本会检查前置依赖(缺少什么就打印一行明确的修复命令),并询问使用哪种梦境提取器——通过你的 Max 套餐调用 Claude Sonnet(安装最轻量)、以内置的本地模型作为 Sonnet 的自动回退,或单独使用内置的本地模型(无需任何套餐)。随后它会启动整套服务,为所选客户端完成接入(会话开始时的简报钩子——它会在每次会话中传递记忆循环指导——以及 MCP 传输注册),并对守护进程做健康检查。该脚本是幂等的:随时可以重复执行;`--extractor <mode>` 可用于切换提取器配置。

守护进程启动后,Claude Code 的**插件**会添加会话开始时的记忆简报、常驻记忆循环指导,以及 `/dream` 与 `/memory-status` 命令——MCP 服务器本身由安装脚本注册,因此插件绝不会重复注册它的工具:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex 则直接注册该服务器:

```bash
codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
```

之后,在任意一种编码智能体中说一句:*“记住我的 staging 服务器是 haze-02”*——几天后开启一个全新会话,再问一句:*“哪台是 staging 服务器?”*,答案就会从记忆中被找回。你可以在 Cortex Console(`http://127.0.0.1:8765/ui/`)中浏览一切。

## 工作原理

该智能体在工作过程中会逐条存入声明(`memory_store`、`memory_fact_set`);一套新颖度门控的存储机制会剔除近似重复的内容。在会话之间,**dream** 会把记忆流蒸馏为规范事实、图谱关系与过程性经验教训。每次会话开始时,简报都会注入记忆中尚不确定的部分、过往工作的经验教训,以及你上次停下的地方。检索会将记忆带上的语义搜索与规范事实库结合起来,使已更正的答案胜过过时的答案。

## 文档(英文)

权威且始终保持最新的文档使用英文撰写:

- [README](../../README.md) —— 完整的安装、接入、工具与故障排查说明
- [配置](../guide/configuration.md) · [检索](../guide/retrieval.md)
  · [梦境机制](../guide/dreaming.md) · [会话片段](../guide/episodes.md)
  · [记忆模型](../guide/memory-model.md) · [性能基准](../guide/benchmarks.md)

本页是面向中文读者的翻译版引言,已同步至下方标注版本的英文 README;如两者内容存在出入,以英文文档为准——英文文档是权威版本。
