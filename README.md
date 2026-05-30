# AI PR Review 助手

基于大语言模型的 GitHub Pull Request 代码评审工具，帮助开发者提升 Review 效率与代码质量。

## 功能概述

- **PR 变更总结**：自动获取 PR 的代码变更，生成结构化的变更摘要（概述、关键变更点、受影响模块、整体风险评估）
- **风险代码识别**：多维度识别潜在问题，按严重程度分级（P0 关键 / P1 高 / P2 中 / P3 低），覆盖安全漏洞、逻辑错误、性能隐患、代码规范
- **Review 建议生成**：针对每个风险项提供具体的改进建议和代码示例
- **分析模式自适应**：根据 PR 变更量自动调整分析深度（trivial / simple / normal / large），避免小 PR 过度分析、大 PR 信息遗漏
- **Web 交互界面**：输入 PR URL 即可获得结构化的 Review 报告

## 技术栈与依赖

本项目基于 Python 开发，核心依赖如下：

| 依赖 | 版本 | 用途 | 许可证 |
|------|------|------|--------|
| [FastAPI](https://github.com/tiangolo/fastapi) | 0.115.x | Web 框架，提供 REST API | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.30.x | ASGI 服务器 | BSD-3 |
| [httpx](https://github.com/encode/httpx) | 0.27.x | HTTP 客户端，调用 GitHub API | BSD-3 |
| [openai](https://github.com/openai/openai-python) | 1.51.x | LLM API 调用（兼容 OpenAI 协议） | Apache 2.0 |
| [Pydantic](https://github.com/pydantic/pydantic) | 2.9.x | 数据验证与序列化 | MIT |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.0.x | 环境变量加载 | BSD-3 |

完整依赖清单见 `backend/requirements.txt`。

## 原创功能模块说明

本项目所有核心业务逻辑均为独立原创实现，未直接复用任何现有代码评审工具。各模块原创设计如下：

| 模块 | 文件 | 原创设计要点 |
|------|------|-------------|
| 配置管理 | `backend/config.py` | 多层级配置模型（GitHub/LLM/Context），支持 .env → 环境变量 → 默认值的优先级链 |
| 数据模型 | `backend/models/schemas.py` | 完整定义 PR 数据、四级风险项、变更总结、分析结果的 Pydantic 结构，字段级中文注释 |
| GitHub 集成 | `backend/services/github_service.py` | URL 正则解析、分页获取、自动重试+指数退避、Rate Limit 友好提示 |
| 上下文构建 | `backend/utils/context_builder.py` | 四模式自动判定（trivial/simple/normal/large）、Token 预算分层管理、大文件截断策略 |
| LLM 分析管道 | `backend/services/llm_service.py` | 两阶段分析 Pipeline（总结→风险识别）、自我反驳机制、白名单误报控制、安全 JSON 解析 |
| API 路由 | `backend/routers/review.py` | FastAPI 路由编排，串联 GitHub 数据拉取 → 上下文构建 → LLM 分析 → 结果返回 |
| Web 界面 | `frontend/` | 原生 HTML/CSS/JS（GitHub Dark 风格），无前端框架依赖 |

## 系统架构

```
用户 → Web UI → FastAPI → GitHubService (拉取 PR 数据)
                            ↓
                       ContextBuilder (模式判定 + Token 裁剪)
                            ↓
                       LLMService (两阶段分析 Pipeline)
                            ↓
                       结构化 Review Report → Web UI 展示
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- Git

### 2. 安装依赖

```bash
cd AI-PR-Review
pip install -r backend/requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入 LLM API 密钥：

```ini
LLM_API_KEY=your-api-key
LLM_API_BASE=https://api.deepseek.com/v1   # 或其他兼容 OpenAI 协议的服务
LLM_MODEL=deepseek-chat                     # 或 gpt-4o-mini / claude-3-5-sonnet
GITHUB_TOKEN=                               # 可选，公开仓库不需要
```

### 4. 启动服务

```bash
cd backend
python main.py
```

### 5. 使用

浏览器打开 `http://localhost:8000`，输入 GitHub PR URL 即可开始分析。

## 模型选择说明

- **推荐主模型**：DeepSeek-V3（`deepseek-chat`）—— 代码理解能力强、128K 上下文、性价比高
- **降级备选**：GPT-4o-mini —— 轻量 PR 场景够用，成本极低
- 项目通过 OpenAI 兼容协议支持任意模型切换，只需修改 `.env` 中的 `LLM_API_BASE` 和 `LLM_MODEL`

## 上下文获取策略

- 优先级分层：代码 diff（核心）> 文件元信息 > PR 描述 > commit message
- Token 预算管理：总预算 16K tokens，diff 占 50%，元信息 30%，prompt 模板 20%
- 大文件裁剪：超出预算时按变更行 ±15 行上下文保留，远端代码按优先级丢弃
- 局限性：当前版本不做 AST 级跨文件依赖分析（受 72h 开发周期约束）

## 未来扩展方向

- **短期**（1-2 周）：CI/CD 集成（GitHub Actions）、自动发 Review Comment、私有仓库支持
- **中期**（1-2 月）：多语言 AST 深度分析、项目级 RAG 知识库、团队自定义规则引擎
- **长期**（3-6 月）：多 Agent 协作分析、增量 Review、VS Code / JetBrains 插件

## 许可证

MIT License
