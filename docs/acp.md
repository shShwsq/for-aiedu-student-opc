# ACP(Agent Client Protocol)说明

本文档说明 ACP 是什么、核心协议内容,以及本项目如何用 ACP 接入三个 CLI agent(Qoder、DeepSeek Harness、Codex)。

---

## 1. ACP 是什么

ACP(Agent Client Protocol)是一个开放协议,由 Zed 编辑器团队发起,用于规范**客户端**(代码编辑器、IDE、后端服务)与**AI coding agent**(CLI 智能体)之间的通信。官方资源:

- 官网:<https://agentclientprotocol.com>
- 仓库:<https://github.com/agentclientprotocol/agent-client-protocol>
- 当前稳定协议版本:**1**(数字)

### 1.1 传输与编码

- **JSON-RPC 2.0** over **stdio**(子进程的 stdin/stdout)
- 消息为 **newline-delimited JSON**(每行一个 JSON 对象)
- 请求带 `id`,响应匹配同一 `id`;通知(notification)无 `id`

```jsonc
// 请求(客户端 → agent,写入 stdin)
{"jsonrpc": "2.0", "method": "initialize", "params": {...}, "id": 1}
// 通知(agent → 客户端,从 stdout 逐行读出,无 id)
{"jsonrpc": "2.0", "method": "session/update", "params": {...}}
// 最终响应(匹配 id,标志本轮请求结束)
{"jsonrpc": "2.0", "result": {...}, "id": 1}
```

### 1.2 解决什么问题

各家 CLI agent(Qoder、Codex、Claude Code、Gemini CLI 等)各有不同的启动参数、输出格式和交互方式。ACP 把"驱动一个 agent 完成任务"抽象为统一接口:

- 客户端不需要为每个 agent 写一套解析逻辑,只需实现一份 ACP 客户端
- agent 侧只需实现一次 ACP 服务,即可被所有兼容客户端(编辑器/平台)接入
- 协议原生支持**流式输出**(增量推送思考、正文、工具调用)与**权限确认**(危险命令弹窗审批)

---

## 2. 核心协议内容

### 2.1 客户端 → agent 的请求方法

| 方法 | 作用 |
|------|------|
| `initialize` | 握手:交换协议版本与能力;若 agent 要求认证,响应中含 `authMethods` |
| `authenticate` | 用某个 `methodId` 完成认证(凭证经环境变量注入,不走协议明文) |
| `session/new` | 创建会话,`params` 必含 `mcpServers`(可为空数组),可选 `cwd` 工作目录 |
| `session/prompt` | 发送任务 prompt,流式接收 `session/update` 通知,直到最终响应 |
| `session/cancel` | 取消正在进行的 prompt |
| `session/set_config_option` | 运行时切换配置(如 `mode`、`model`、`thinking`),无需重启 CLI |

> `session/prompt` 的 prompt 内容采用 **MCP content 数组格式**(如 `[{"type":"text","text":"..."}]`),不是 OpenAI 的 `{"role","content"}` 格式。

### 2.2 agent → 客户端的通知:`session/update`

一次 `session/prompt` 内部可包含多轮 ReAct 迭代(思考 → 工具调用 → 工具结果 → 思考 → ...),每次增量都通过 `session/update` 通知推送:

```jsonc
{
  "method": "session/update",
  "params": {
    "sessionId": "...",
    "update": {
      "sessionUpdate": "agent_message_chunk",  // 见下表
      "content": {"type": "text", "text": "..."}
    }
  }
}
```

常见 `sessionUpdate` 类型:

| sessionUpdate | 含义 |
|---------------|------|
| `agent_message_chunk` | 助手正文增量(delta) |
| `thought_chunk` | 思考/推理过程增量 |
| `tool_call` | 工具调用开始(含 toolCallId、标题、rawInput) |
| `tool_call_update` | 工具调用状态更新(如 `completed`,参数可能增量构建) |
| `plan` | 计划/待办清单 |
| `error` | 错误信息 |

### 2.3 权限确认:`request_permission`

这是 ACP 中**反向**的 JSON-RPC 请求(agent → 客户端):agent 检测到危险命令时,主动发 `request_permission`(带 `id`、`tool_call` 详情、可选项 `options`),客户端展示给用户审批,再把结果(`{"outcome": "selected", "option_id": "allow_once"}` 或 `{"outcome": "rejected"}`)作为响应写回 agent 的 stdin。

> 本项目也可用 `--yolo` / bypass 模式让 agent 跳过权限确认(沙箱内运行,安全性由沙箱保证),此时 agent 不会发 `request_permission`。

---

## 3. 本项目为什么用 ACP

本项目需要接入外部 CLI agent 作为执行器,ACP 提供:

1. **统一接入层**:一份 ACP 客户端代码驱动所有 agent,新 agent 只需配置(启动命令、凭证环境变量),无需改核心逻辑
2. **流式体验**:思考、正文、工具调用实时增量推送,适合 SSE 转发给前端
3. **凭证安全**:凭证经环境变量注入沙箱内的 bridge 进程,CLI 子进程继承,命令行不出现明文
4. **多轮会话**:session 机制天然支持追问/续跑

---

## 4. 本项目接入的三个 CLI agent

本项目接入以下三种 CLI agent:

### 4.1 Qoder CLI —— 原生 ACP

| 项 | 内容 |
|----|------|
| 源码位置 | 无本地源码 |
| 启动方式 | `qodercli --acp --yolo`(`--acp` 启动 ACP stdio 服务,`--yolo` 跳过权限确认) |
| 认证 | PAT(Personal Access Token),经环境变量 `QODER_PERSONAL_ACCESS_TOKEN` 注入,`initialize` 后需 `authenticate` |
| 模型 | 由 Qoder 账号配额管理,经 `--model` CLI 参数指定(如 `DeepSeek-V4-Flash`),后端不直接管理 LLM 调用 |
| 安装 | `npm install -g @qoder-ai/qodercli` |

特点:CLI 内部自主完成 ReAct 循环,后端只发 prompt、收事件,是最标准的 ACP 接入形态。

### 4.2 DeepSeek Harness —— 原生 ACP

| 项 | 内容 |
|----|------|
| 源码位置 | <https://github.com/deepseek-ai/deepseek-harness>(DeepSeek AI 开源,MIT,默认分支 `master`) |
| ACP 实现 | `packages/acp/acp`(`@deepseek-ai/dsh-acp`),基于官方 `@agentclientprotocol/sdk` |
| 启动方式 | `dsh --profile acp`(stdio JSON-RPC 服务器,随附的 ACP profile) |
| 认证 | `authenticate` 立即成功,服务器本身不要求认证;LLM 凭证经 `DEEPSEEK_API_KEY` 环境变量注入(+ 可选 `DEEPSEEK_BASE_URL`,自部署/代理端点) |
| 模型 | 无 `--model` CLI 参数,session/new 后经 `session/set_config_option` 设置(configId: `model` / `reasoning_effort`),模型如 `deepseek-v4-flash` / `deepseek-v4-pro`;不设置时用 profile 默认模型 |
| 权限模式 | `DSH_PERMISSION_MODE` 环境变量注入:默认 `workspace-write`(危险命令发 `request_permission` 弹窗确认);`always_approve` 时注入 `danger-full-access` 跳过审批 |
| 安装 | Node.js ≥ 20,`npm install -g @deepseek-ai/dsh`(源码运行需 pnpm) |

特点与限制(见其 `packages/acp/acp/README.zh.md`):

- 一个连接可并发多个独立会话;支持 `session/list` / `session/resume` / `session/close`(持久化会话)
- 只发送**标准语义更新**(已提交的 assistant 消息、thought、通用工具生命周期、配置变化、上下文用量)
- 仅自动化界面:不支持 `session/load`、会话删除/fork、mode、终端视图、elicitation 等交互式扩展
- 仅一个主 workspace;提示词图片仅支持光栅格式(PNG/JPEG/WebP/GIF);工具仅支持 MCP 工具
- stdout 只承载协议流量,日志必须走 stderr

### 4.3 Codex CLI —— 无原生 ACP,需桥接

| 项 | 内容 |
|----|------|
| 源码位置 | <https://github.com/openai/codex>(OpenAI 官方开源,Apache-2.0,Rust 实现,默认分支 `main`) |
| 原生 ACP | **不支持**,但提供非交互模式:`codex exec --json` 输出 JSONL 事件流;`codex exec resume <thread_id>` 恢复会话实现多轮对话 |
| 认证 | API Key 经环境变量注入;模型/provider 经 `~/.codex/config.toml` 配置 |
| 通信协议 | 仅支持 OpenAI **Responses API**(wire_api 固定 `responses`),端点必须实现 `/v1/responses`;只支持 `/v1/chat/completions` 的中转/Ollama/vLLM 无法直连 |
| 安装 | Node.js ≥ 16,`npm install -g @openai/codex` |

JSONL 事件(定义于 `codex-rs/exec/src/exec_events.rs`)与 ACP 通知的映射关系:

| Codex JSONL 事件 | ACP 通知 |
|------------------|----------|
| `item.started/updated`(agent_message) | `agent_message_chunk`(增量 delta) |
| `item.started/updated`(reasoning) | `thought_chunk`(增量 delta) |
| `item.started`(command_execution / file_change) | `tool_call` |
| `item.completed`(command_execution / file_change) | `tool_call_update`(completed) |
| `item.started/updated`(todo_list) | `plan` |
| `item.started/updated`(mcp_tool_call / web_search) | `tool_call` + `tool_call_update` |
| `turn.completed` | ACP 最终响应(本轮结束) |
| `turn.failed` / `error` | ACP 错误响应 |

### 4.4 三者对比

| | Qoder | DeepSeek Harness | Codex |
|---|---|---|---|
| ACP 支持 | 原生 | 原生(dsh-acp 插件) | 无,需桥接层翻译 |
| 启动命令 | `qodercli --acp --yolo` | `dsh --profile acp` | `codex exec --json`(经 bridge 包装) |
| 认证 | PAT(env 注入 + authenticate) | 无协议认证,`DEEPSEEK_API_KEY`(+ 可选 `DEEPSEEK_BASE_URL`) | API Key(env + config.toml) |
| 模型配置 | `--model` CLI 参数 | 配置项 / `set_config_option` | `config.toml` |
| 多轮会话 | session 机制(bridge 驻留) | 持久化会话 + `session/resume` | `exec resume <thread_id>` |
| bridge 需求 | 通用 stdio 桥接即可 | 通用 stdio 桥接即可 | 专用翻译 bridge |

---

## 5. 架构

本项目的做法:**不在后端直接 spawn CLI**,而是在沙箱内运行一个轻量 HTTP ↔ stdio 桥接服务,后端统一用 HTTP/SSE 与之通信。相关源码(本项目 `backend/app/agents/`):

```
后端(FastAPI)
   │  HTTP POST /rpc(JSON-RPC 请求体)
   │  ◄─ SSE 流(通知 + 最终响应 + permission_request / stream_error 事件)
   ▼
沙箱内 bridge(acp_bridge.py,监听 8088)
   │  stdin ▲ │ ▼ stdout(newline-delimited JSON-RPC)
   ▼
ACP CLI 子进程(qodercli --acp --yolo / dsh --profile acp)
```

### 5.1 各模块职责

| 文件 | 职责 | 本项目取舍 |
|------|------|-----------|
| `acp_bridge.py` | 通用 HTTP ↔ stdio 桥:spawn 任意 ACP CLI 子进程,`POST /rpc` 把 JSON-RPC 写入 stdin、stdout 逐行读出并以 SSE 流式返回;处理 `request_permission` 双向审批;`GET /health` 健康检查 | **采用**(驱动 Qoder、DeepSeek Harness) |
| `codex_bridge.py` | Codex 专用桥:对外暴露同样的 ACP HTTP 接口,对内把 ACP 方法翻译为 `codex exec --json` 调用,把 JSONL 事件翻译为 ACP 通知 | **采用**(驱动 Codex) |
| `acp_base.py` | 共享基础设施:`ACPClient`(initialize / authenticate / session/new / prompt / cancel 等方法)、bridge 生命周期管理、凭证加载与加密、`_ACPCollector`(session/update → 前端事件翻译)、idle 看门狗、原始报文录制 | **采用**(改造为三 agent) |
| `qoder_cli_agent.py` 等 wrapper | 各 agent 特有逻辑(如 Qoder 的 print 模式 PAT 诊断、测试连接时的最便宜模型) | 参考,本项目实现 qoder_cli / deepseek_cli / codex_cli 三个 wrapper |
| `registry.py` | agent 注册表:显示名、凭证字段(动态生成表单)、沙箱配置(bin / install_cmd / acp_args / credential_env)、executor 模块映射 | **采用**,仅注册 qoder_cli / deepseek_cli / codex_cli 三项 |

### 5.2 关键设计点(继承自上游参考实现并经其验证)

1. **bridge 驻留**:bridge 与 CLI 进程随沙箱会话存活,跨轮次/追问复用(省去每次启动 + initialize + session/new 约 25s);沙箱销毁时连带回收
2. **凭证注入**:用户凭证加密存储,运行时映射为环境变量(如 `QODER_PERSONAL_ACCESS_TOKEN`、`DEEPSEEK_API_KEY`、`CODEX_API_KEY`)注入 bridge 进程,CLI 子进程继承,命令行与协议中均无明文
3. **串行协议**:ACP over stdio 是串行的(同一时刻一个请求),bridge 用锁保护 send+collect 全程;Codex 的 exec 也是每次 prompt 起一个新进程
4. **流式翻译**:`session/update` 的 `agent_message_chunk` / `thought_chunk` / `tool_call` / `plan` 分别映射为前端的正文增量、思考增量、工具卡片、计划事件;按 `tool_call` 切分 ReAct 迭代
5. **挂死/崩溃兜底**:prompt 期间长时间无数据事件时分级 idle 超时(工具执行中放宽、等模型输出收紧),超时发 `session/cancel` 并用已累积输出收尾,不直接 fail 任务;CLI 崩溃/连接中断(SSE 流结束但未收到 JSON-RPC 最终响应)时,bridge 关流前推 `event: stream_error` 携带原因(进程退出/EOF/读失败),后端抛 `ACPStreamAborted` 并走同款截断善后,summary 标注"本轮输出不完整"让 agent2 知情,不把崩溃当作正常完成
6. **新增 agent 成本**:在 registry 注册(bin、acp_args、credential_env、bridge_script)+ 写一个薄 wrapper,其余全复用

---

## 6. 参考文件索引

下表为上游开源仓库的 GitHub 地址(`references/` 目录仅为本地参考副本,不随本仓库提交)。

| 内容 | 地址 |
|------|------|
| ACP 官方规范 | <https://agentclientprotocol.com>(协议版本 1) |
| DeepSeek Harness ACP 包说明 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.zh.md> |
| DeepSeek Harness 仓库根文档 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md> |
| Codex JSONL 事件定义 | <https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs> |
| Codex 仓库根文档 | <https://github.com/openai/codex/blob/main/README.md> |
