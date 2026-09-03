# AGENTS.md — Bulltrader 项目 Agent 协作约定

本文件是所有 AI Agent（Codex / Trae / 其他子 Agent）在本仓库协作时**必须遵守**的强制规范。聚焦于**项目记忆的读写约定**，与 `SKILL.md`（系统功能手册）分工互补。

---

## 1. 记忆系统：mem0 MCP（强制）

本项目**所有**项目记忆的读写必须通过 **mem0 MCP** (`server_name: mcp_mem0`) 调用，不得绕过去写本地文件或自建记忆存储。

### 1.1 身份凭证（固定值，勿改）

| 字段      | 值           | 说明                                                       |
| --------- | ------------ | ---------------------------------------------------------- |
| `user_id` | `soj`        | 项目唯一用户标识                                           |
| `app_id`  | `bulltrader` | 应用标识（mem0 工具入参对应 `app_id`，**不是** `appname`） |

**每次写入（`add_memory`）和检索（`search_memories`** **/** **`get_memories`）都必须显式带上这两个凭证**，避免记忆被混入其他项目的命名空间。

### 1.2 可用工具速查

| 工具                                | 用途                                             | 关键参数                                                                     |
| ----------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------- |
| `add_memory`                        | 写入一条记忆（偏好/事实/决策/踩坑）              | `text`、`user_id="soj"`、`app_id="bulltrader"`、可选 `metadata` / `infer`    |
| `search_memories`                   | 语义检索（按自然语言查询）                       | `query`、`filters`（如 `{"AND":[{"user_id":"soj"}]}`）、`top_k`、`threshold` |
| `get_memories`                      | 分页浏览（无关键词时用）                         | `filters`、`page`、`page_size`                                               |
| `get_memory`                        | 按 `memory_id` 取单条                            | `memory_id`                                                                  |
| `update_memory`                     | 更新已有记忆的文本或 metadata                    | `memory_id`、`text` / `metadata`                                             |
| `delete_memory`                     | 删除单条（用户确认后）                           | `memory_id`                                                                  |
| `list_entities` / `delete_entities` | 实体级操作                                       | 详见 MCP 工具 schema                                                         |
| `list_events` / `get_event_status`  | 异步写入事件轮询（`add_memory` 返回 `event_id`） | `event_id`                                                                   |

### 1.3 写入约定（`add_memory`）

1. **`text`** **写成一句完整自然语言**，包含足够上下文让未来检索能命中。例：
    - 好：`"intraday_t 策略在缩量环境下阈值需重调，当前回测 -1.9% vs 买入持有 +5.1%"`

    - 差：`"阈值要改"`（信息量不足）

2. **优先用** **`text`** **字段**；只有当涉及多轮对话片段时才改用 `messages`（`role`/`content`）。
3. **结构化字段放** **`metadata`**，例如 `{"category": "decision", "module": "condition_orders", "tag": "踩坑"}`，便于后续过滤。
4. **默认** **`infer=true`**（让 mem0 抽取结构化记忆）；只有当要原文存档时才显式传 `infer=false`。
5. **写入后立即记录返回的** **`memory_id`** **/** **`event_id`**，以便后续更新或轮询状态。

### 1.4 检索约定（`search_memories`）

1. **必须传** **`filters`**，至少包含 `{"AND": [{"user_id": "soj"}, {"app_id": "bulltrader"}]}`（mem0 会自动注入 `user_id`，但 `app_id` 需手动加）。
2. **`query`** **用自然语言描述要找什么**，不要塞关键词堆。
3. **默认** **`top_k=10`、`threshold=0.1`**；需要召回更多时显式调大 `top_k`，需要高置信时调高 `threshold`。
4. **`rerank=false`** **默认**，只有当结果噪声大时再开（增加 200-400ms 延迟）。
5. **检索结果是数组**，每条含 `memory`（文本）、`id`、`score`、`metadata`，按相似度倒序。

### 1.5 与本地文件记忆的关系

- **mem0（本节）**：跨会话的语义记忆，**首选**用于偏好、决策、踩坑结论。

- **本地** **`~/.trae-cn/memory/...`（`project_memory.md`** **/** **`topics.md`）**：Trae 内置的项目记忆，按文件追加，无语义检索。

- 两套**并存**：重要结论写入 mem0 后，可同步在本地文件留个一行索引（`已写入 mem0：<memory_id> <摘要>`）以便回看，但**不要**把 mem0 的全部内容复制回本地文件。

---

## 2. 工作流约定

1. **会话开始时**：先 `search_memories` 检索本会话主题相关记忆（如 `query="condition_orders 状态机实盘验证"`），避免重复踩坑。
2. **会话结束/关键决策时**：用 `add_memory` 写入新的决策、踩坑结论或偏好变更。
3. **改动架构或数据源选型前**：必须先检索相关记忆，避免推翻已定结论（如 eltdx 7709、东财 push2 限频等已踩坑结论）。
4. **会话结束时**: 调用以下命令发送语音提醒用户 `qwen-tts "traework已经完成任务,请验收."`

---

## 3. 其他

- 本文件为强制规范，与 `SKILL.md` 同级优先；冲突时以 `SKILL.md` 的功能细节为准，以本文件的**记忆调用约定**为准。

- 如 mem0 MCP 不可用或返回错误，先 `get_event_status` 轮询 `event_id`；持续失败时降级写入本地 `~/.trae-cn/memory/.../project_memory.md` 并在下次会话补写回 mem0。
