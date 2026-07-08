# query-indicator

华泰证券金融指标与行情综合检索 OpenClaw skill —— 让 LLM 用自然语言完成金融指标查询、行情数据检索、财务估值对比等操作。

通过本 skill，LLM 可以理解用户的指标查询意图（"看看华泰证券最新价"、"南京中达昨天和前天的最高价"），并通过后端服务获取专业结果。所有响应统一结构，对模型友好且对人类可读。

## 安装

前提：Python 3.9 或以上（系统 Python 需已安装 `requests` 包）。

```bash
clawhub install query-indicator
```

或克隆本仓库到 OpenClaw skills 目录后，通过 OpenClaw 自动识别。

## 配置

使用前需配置以下环境变量:

| 变量 | 必填 | 说明 |
|---|---|---|
| `QUERY_INDICATOR_SERVICE_URL` | 否 | 后端服务地址（默认 `https://ai.zhangle.com`） |
| `HT_APIKEY` | 是 | 认证密钥 |

```bash
export HT_APIKEY="<your-api-key>"
```

## 工具

| 工具 | 说明 |
|---|---|
| `queryIndicator` | 查询金融指标、行情数据或财务估值 |

详细参数、返回字段见 [`SKILL.md`](SKILL.md)。

## 直接调用脚本

OpenClaw 内核会按 `SKILL.md` 中的"**执行**"指令调用本 skill。所有工具走统一入口：

```bash
python3 query_indicator.py <toolName> [args]
```

开发者也可直接命令行调用：

```bash
# 查询金融指标
python3 query_indicator.py queryIndicator --query "看看华泰证券最新价"

# 查看参数说明
python3 query_indicator.py queryIndicator --help
```

所有调用输出统一的 JSON 结构 `{ ok, data, error }`，详见 SKILL.md "响应结构"段。

## 设计原则

### 查询保真
不拆分、不改写、不补充用户的原始提问，避免信息丢失或歧义。"南京中达昨天和前天的最高价"单次传入，不拆为两次调用，不将"昨天"替换为具体日期。

### 响应统一
成功时 `data.answer` 已是结构化 Markdown，可直接展示。失败时 `error.category` 让模型立刻知道错误性质（校验/业务/网络），决定是否重试。

### 后端承载业务逻辑
指标解析、数据聚合等业务逻辑由后端服务处理，skill 层仅做 HTTP 转发。

## 仓库结构

```
query-indicator/
├── SKILL.md              # 给 OpenClaw 内核 + LLM 的契约文档
├── README.md             # 本文件
├── query_indicator.py    # 配置、HTTP 客户端、工具函数、CLI 调度入口
└── pyproject.toml
```

## 安全建议

- 后端服务地址仅存于环境变量，不写进代码或日志
- skill 层不持久化任何用户数据
- 不提交 `.env` 文件到版本控制

## 维护

本项目由华泰证券维护。

**Authors**: Huatai Securities

## 许可证

MIT
