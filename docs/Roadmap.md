# SecondLook 开发 Roadmap

> 适合 vibe coding 的迭代路线:每个阶段都是一个**能独立跑通、能立即看到效果**的闭环。做完一个阶段先玩一下,有手感了再进下一个。

---

## 当前总览

| 阶段 | 名称 | 状态 |
|------|------|------|
| 0 | 最小骨架跑通 | ✅ 已完成 |
| 1 | 单 react_agent 跑通真实审计 | ✅ 已完成 |
| 2 | 加沙箱,代码在沙箱里跑 | ✅ 已完成 |
| 3 | 加 Semgrep 和 CVE 查询 | ✅ 已完成 |
| 4 | 引入 agent2(检查助手),双智能体协作 | ✅ 已完成 |
| 5 | SKILL 机制 | ✅ 已完成 |
| 6 | 用户系统与模型配置 | ✅ 已完成 |
| 7 | 网站前端 | ✅ 已完成 |
| 8 | 微信小程序 | ⬜ 未开始 |
| 9 | 生产化与上线 | 🔨 部分完成 |
| 10 | 练习题与自适应练习 | ✅ 已完成 |
| 11 | 工作区变更(检查点评估后移除) | ✅ 已完成 |

### 超出原规划已实现的功能

以下功能在迭代过程中自发产生,已落地到代码中:

#### 执行器与外部 CLI 集成

- **执行器抽象层(ExecutorAgent)**:把"AI助手"抽象为统一接口,支持 builtin(内置 react_agent)和外部 CLI agent(通过 ACP 协议通信)两种 provider。新增 agent 类型只需在 registry 注册,无需改核心代码。
- **ACP Bridge**:HTTP ↔ stdio 桥接服务,运行在沙箱内,让外部 CLI agent(如 Qoder CLI / DeepSeek Harness CLI / Codex CLI)通过 ACP 协议接入。
- **Qoder CLI Agent**:通过 ACP 协议调用 Qoder CLI 作为 AI助手(agent1),模型由 CLI 账号配额管理,不走后端 LLM 配置。
- **DeepSeek CLI Agent**:通过 ACP 协议调用 DeepSeek 开源的 DeepSeek Harness CLI(简称 dsh,`dsh --profile acp`)作为 AI助手(agent1),模型经 `session/set_config_option` 在 session/new 后设置(model / reasoning_effort),权限模式经 `DSH_PERMISSION_MODE` 环境变量注入(默认 workspace-write,always_approve 时 danger-full-access),凭证 `DEEPSEEK_API_KEY`(+ 可选 `DEEPSEEK_BASE_URL`,支持自部署端点)。
- **Codex CLI Agent**:通过 ACP 协议调用 OpenAI Codex CLI。Codex 不原生支持 ACP,通过专用的 `codex_bridge.py` 翻译 `codex exec --json` 的 JSONL 事件流为 ACP 通知。凭证经 `CODEX_API_KEY` 环境变量注入,`~/.codex/config.toml` 写入 `approval_policy=never` + `sandbox_mode=danger-full-access` 支持非交互模式。**注意**:`codex exec --json` 是非交互模式,不支持 `per_command` 命令确认(会自动降级为 `always_approve` 并警告)。
- **codex_bridge.py**:Codex 专用桥接脚本,处理 Codex 特有的事件流语义(ErrorItem 通知翻译为 thought_chunk 而非 error、stderr 累积到错误消息等),所有 POST `/rpc` 响应以 SSE 流式返回。

#### 双智能体协作增强

- **场景降级**:checklist 不再从场景固定读取,改为 agent2 第 0 轮动态生成 + 用户编辑确认。prompt 通用化,工具全部开放,结果结构通用化。
- **任务暂停/恢复**:用户可暂停运行中的任务,后台线程在检查点(迭代边界/工具调用前)阻塞,恢复后继续。
- **用户补充消息**:任务运行中/完成后,用户可追加消息触发新一轮协作(resume_audit_with_message)。
- **跨轮记忆传递**:三级压缩策略(完整 → 丢工具摘要 → LLM 压缩早期轮次),控制 token 成本。
- **循环检测**:滑动窗口检测重复工具调用(连续相同 + 交替循环),打破死循环。
- **Plan 状态管理**:react_agent 在思考中输出 `<plan>` 清单,代码维护状态,跨轮续接避免重复规划。
- **verifier_agent(实验性)**:独立的验证智能体,agent2 在评估覆盖度后可调用它在已部署测试环境动态验证 agent1 的发现(如确认 SQL 注入是否真实可利用)。独立 ReAct 循环 + 独立工具集(`http_request` 在沙箱内 urllib 执行 + `run_python_code`),支持 `per_action`(每个动作弹窗确认)/ `direct`(直接执行)两种授权模式。支持登录 token 注入(auth_profile,label 选择身份,LLM 永不见 token 明文)。
- **协作策略设置页**:用户可配置评估频率(每轮 / 每两轮 / 仅最后)、验证权限(是否允许 agent2 自行验证 + 授权模式默认值 + verifier 测试环境 URL + 多个登录 token)、AI助手命令确认模式(自动批准 / 逐命令确认,对内置 react_agent 与 CLI 执行器均生效)。
- **AI助手命令确认**(executor_command_confirm):控制AI助手(内置 react_agent + CLI:qoder/deepseek/codex)执行危险命令时是否弹窗确认,防容器破坏与资源耗尽。两条独立机制:**内置 react_agent** 走 `sandbox_tools.run_command` 的 `_PendingCommandConfirm` 机制(与 local 模式危险命令确认同源,SSE 事件 `command_confirm`),通过 `react_agent.py` → `set_current_task` → `_CURRENT_EXECUTOR_COMMAND_CONFIRM` ContextVar → `execute_tool` 自动注入;**CLI AI助手** 走 ACP `request_permission` 机制(bridge SSE 推 `permission_request` 事件 → 前端 `CommandConfirmDialog` 弹窗 → `POST /tasks/{id}/permission_response` 回写)。`always_approve`(默认):内置 react_agent 在 sandbox 下直接执行,CLI 注入 YOLO/never 配置跳过审批;`per_command`:两条路径都推确认。local 模式下 dangerous 命令始终推确认(无视此字段)。Codex 受非交互模式限制仅支持 `always_approve`,`per_command` 时自动降级并警告。用户级默认在协作策略页设置,任务级可在新建任务页覆盖(builtin 与 CLI 执行器均显示)。

#### 记忆与技能系统

- **记忆管理**:三类长期记忆统一管理(用户偏好 User Profile / 全局记忆 UserMemory / 项目记忆 Project)。agent2 在 system prompt 注入精简版,agent1(内置 react_agent 或 CLI agent)在沙箱写入完整项目记忆供 `read_file` 查阅。前端记忆管理页支持查看 / 编辑 / 删除。
- **用户技能上传**:用户可上传自定义 skill(SKILL.md 文件),隔离存储(per-user 目录),仅本人任务可用。系统内置 skill 全局共享。任务创建时可选允许调用的 skill 集合(`allowed_skills`)。

#### 练习题与自适应练习

- **题目生成(generator.py)**:任务 Results 一键生成客观题(单选/判断),LLM 逐条 finding 生成 1~3 题(漏洞识别 / 成因判断 / 修复选择),严格 JSON + 重试 + 字段校验 + sha256 去重,草稿确认制(draft → active)。
- **SM-2 遗忘曲线(sm2.py)**:答对 quality=4 / 答错 quality=1,EF 与间隔序列(1 → 6 → 前值×EF)标准实现,`due_at` 驱动到期复习。
- **综合选题(selector.py)**:到期复习优先 > 薄弱点强化 > 难度匹配 > 新知识引入的加权打分,含同知识点 ≤60%、复习题占比 ≥50%、冷启动取难度 ≤2 新题等约束。
- **四主题出题(自动匹配)**:网络安全 / 架构设计 / 通用代码能力 / 合同文书四套提示词;主题不再是用户级设置,而是出题时逐 finding 自动匹配(规则先行 + LLM 一次批量兜底,失败降级 security);沙箱未销毁时注入材料(源码或文书原文) + 迷你工具循环(read_file/search_code/find_files)增强质量;沙箱过期可重新拉取工作区(默认关)。
- **知识点主题手动修正(知识点看板)**:看板每张卡片底部「改主题」下拉可修正自动匹配出错的主题(`PUT /practice/knowledge-points/{key}/topic`);改在知识点级并级联更新其下题目的 `learning_topic`(看板分组与「练这个主题」组卷均按知识点主题命中),目标主题须属于当前用户(含已停用),改后整板刷新让卡片流入新分区。
- **出题模型三级解析**:task 级 > 用户级默认(`practice_settings.default_llm_config_id`)> env 默认;思考模式三态覆盖(follow/on/off)。
- **异步 job + SSE**:出题后台线程执行,`/practice/generate/{job_id}/stream` 推送进度;出题日志落盘 `logs/practice_generate.log`。
- **停止 / 继续出题**(出题进度侧栏):`POST /practice/generate/{job_id}/stop` 置一次性停止标志后立即返回,后台线程在检查点(逐条 finding 之间 / 并发 worker 入口 / 工作区恢复的克隆轮询)协作式收尾,job 终态新增 `cancelled`;**已生成的 draft 先 commit 再返回**(不白烧已付 token),仍可「确认入库」。终态口径只认「确实少跑了活」:停止请求落在最后一条之后(全部已处理)时按 `done` 收口且收尾讲解照常跑,不误报「已停止」也不静默少产出知识点讲解;并发路径下所有已跑完的 worker(含刚触发检查点的那一条)都补落库,只有仍在途的不接收。「继续出题」不复活旧 job,而是用同一 `max_findings` 重发一次 `force_regenerate=false` 的请求 —— 本用户已出过题的 finding 后端会整条跳过,选题顺序确定,所以天然从断点接着跑。`stop_requested` 随 snapshot/job 列表下发,侧栏在生效前显示“正在停止…”(协作式取消可能滞后数十秒)。停止只对出题 job(`manual`/`auto`)开放,知识点讲解 job 的执行线程不查标志 → 409。
- **前端**:PracticeView(练习首页 / 会话答题 / 统计趋势 / 题库管理)、出题进度侧栏、生成确认弹窗、练习设置弹窗;`PRACTICE_ENABLED` 功能开关前后端联动。

#### 工作区变更与智能体策略

- **智能体策略独立表(agent_policies)**:用户级默认从 `user_preferences` JSONB 迁移为独立 1:1 表(agent2 启停 / 协作轮次 / 验证权限),任务级经 `task.params._agent_policy` 覆盖。(其中的“协作轮次 max_rounds”列已随 agent2 后台审查重构移除:初始运行 agent1 单轮即完成,多轮由用户 resume 驱动。)
- **工作区变更捕获(workspace_diff)**:任务完成时捕获已跟踪 + 未跟踪文件合成 git patch,存 `task_artifacts`(kind=git_diff,上限 100 万字符);仓库树快照(kind=repo_tree)兜底;前端任务详情页展示变更区(按行着色、可折叠)。
- (检查点评估与软中断功能曾在本阶段实现,后因价值/成本比不高整体移除:agent2 保留 round 边界完整评估,`AgentPolicy` 表的检查点/打断列一并清理。)
- **终止检查**(`POST /tasks/{id}/review/stop`):后台审查可用户主动停下(有些对话不需要检查)。标志按轮登记在 `review_stop.py`,agent2 在工具循环边界与 LLM 流 chunk 边界(可选 `stop_check` 贯穿 runtime/stream_llm 与 LLMClient.chat_stream,命中即关流)协作式退出;`review_status` 新增 `stopped`(VARCHAR(16) 自由字符串,无需迁移),保留临时结果并改写其“整理中”标题,审查后下游链(记忆归纳/自动出题/预压缩)整体跳过。不用暂停(暂停仅接受 RUNNING,而审查发生在任务已完成后)。

#### 代码审查能力增强

- **质量工具**:`run_lint`(Python ruff / JS eslint)/ `run_coverage`(pytest-cov / vitest),local/sandbox 双模式(缺工具返回指引 / 自动安装)。
- **依赖清单工具**:`list_dependencies` 扫描清单文件返回结构化依赖,串联 `query_cve` 批量查已知漏洞。
- **code_review 场景 skill**:review_concurrency(并发安全)/ review_error_handling(错误处理)/ review_test_quality(测试质量)。

#### 安全与隔离

- **Git 平台抽象层(GitHub / Gitee)**:统一 `GitProvider` 抽象层,支持 GitHub + Gitee 双平台 OAuth 登录与仓库绑定。同一用户可同时绑定两平台,clone 时按仓库 URL 主机自动选用对应 token。取代旧的 `User.github_id` 字段,改为 `UserGitBinding` 表(含一次性数据迁移)。
- **解绑时主动撤销 Token**:解绑 Git 平台时不仅清本地 token,还主动调 GitHub / Gitee 的 revocation API 撤销授权,避免 OAuth 授权态残留导致无法重新绑定。
- **local 模式安全策略**:无沙箱场景(SANDBOX_MODE=local,开发期使用)下通过四层软策略降低风险:
  1. **路径策略**:`.git` 目录 + 配置的只读目录(`.vscode` / `.trae` / `.idea`)写保护
  2. **命令白名单**:按 `SANDBOX_LOCAL_SAFE_COMMANDS` / `SANDBOX_LOCAL_DANGEROUS_COMMANDS` 分类 safe / normal / dangerous
  3. **危险命令前端确认**:dangerous 命令推 `command_confirm` SSE 事件,前端 `CommandConfirmDialog` 弹窗显示完整命令 + 拦截原因 + 拒绝 / 同意按钮,用户拒绝时返回 `[用户拒绝执行此命令]`
  4. **平台原生隔离(可选)**:macOS 用 `sandbox-exec`、Linux 用 `bwrap`,Windows 无原生沙箱跳过
- **GitHub Token 加密存储**:固定 `GITHUB_TOKEN_SECRET` 加密 GitHub / Gitee 的 access token,避免每次重启随机密钥导致解密失败。

#### 前端体验

- **工作区浏览**:前端可浏览 agent1 clone 的工作区文件结构和内容(会话保留期可配
  `WORKSPACE_TTL_AFTER_COMPLETE`,容器被回收后如实报过期并支持重新克隆)。
- **思考链流式推送**:LLM 的 reasoning_content 通过 SSE thinking_delta 事件实时推给前端(打字机效果)。
- **多厂商 LLM 支持**:DashScope(通义千问)/ DeepSeek / 智谱 / 月之暗面 / 豆包 / MiniMax 等,通过 models_catalog.json 统一管理差异。
- **代码审查场景模板**:除安全审计外,新增 code_review 场景(预设提示词 + 推荐 skill)。
- **新手引导(OnboardingTour)**:首次登录播放分步引导,按路由分组(首页 / 任务创建 / 任务详情 / 账号设置),锚点用 `data-onboarding` 属性匹配(比 class 名稳定),版本号递增使老用户重新看到。
- **主题切换**:浅色 / 深色 / 跟随系统三档,顶栏主题切换按钮。
- **帮助文档弹窗**:顶栏问号按钮打开 `help.md` 渲染的帮助弹窗,覆盖所有功能的使用说明。
- **账号设置入口收敛**:账号设置(修改密码 / Git 平台绑定 / 删除账号)只能从顶栏齿轮按钮进入,不在主导航中。
- **任务运行中的多种弹窗**:
  - `QuestionDialog`:agent2 round 0 澄清提问(选择题 + 填空题)
  - `ChecklistReviewDialog`:checklist 编辑确认
  - `VerifyActionDialog`:verifier_agent 的 per_action 授权确认(显示 auth_profile 徽标)
  - `CommandConfirmDialog`:危险命令确认(local 模式 `command_confirm` 事件 + CLI `per_command` 模式 `permission_request` 事件,红色高亮拦截原因)

---

## 阶段 0:最小骨架跑通 ✅

**目标**:后端能起,数据库能连,一个空的 agent 接口能返回假数据。

**做什么**:
- FastAPI 项目骨架 + 路由分层
- SQLite + SQLAlchemy 模型(User / Task / Conversation / Result)
- 一个 `/tasks` POST 接口,接受 repo_url,返回 task_id
- 一个 `/tasks/{id}` GET 接口,返回 task 状态
- 一个假的 agent runner,直接返回"发现 1 个 SQL 注入"这种硬编码结果

**验证方式**:
```bash
uvicorn app.main:app --reload
# curl 提交任务,看 task_id
# curl 查状态,看到假结果
```

**完成标志**:curl 能跑通提交 → 查询 → 拿到假结果的完整链路。

---

## 阶段 1:单 react_agent 跑通真实审计 ✅

**目标**:不要双 agent,先让一个 react_agent 真的能审计一个 GitHub 仓库。

**做什么**:
- 接入 LLM(OpenAI 兼容接口,先写死一个模型)
- react_agent 基础循环:Thought → Action → Observation
- 工具实现(最小集):
  - `clone_repo`:本地 git clone
  - `read_file`:读仓库文件
  - `search_code`:grep 搜索
- agent 跑完把发现写入 Finding 表

**验证方式**:
- 找一个故意有漏洞的小仓库(自己造一个,放几个 SQL 注入和硬编码密钥)
- 提交任务,等 agent 跑完,查结果
- 看到 react_agent 真的找到漏洞

**完成标志**:react_agent 能在一个真实小仓库上跑完,找到预设的漏洞。

---

## 阶段 2:加沙箱,代码在沙箱里跑 ✅

**目标**:把 clone 和工具执行从宿主机挪进 OpenSandbox,真正的隔离。

**做什么**:
- OpenSandbox 接入(创建沙箱、文件推送、命令执行)
- `clone_repo` 改为在沙箱里执行
- 新增 `run_in_sandbox`:agent 可以生成脚本扔进去跑
- react_agent 能用 `run_in_sandbox` 验证漏洞(比如构造 payload 测注入点)

**验证方式**:
- 提交一个仓库,看 agent 是否在沙箱里 clone + 跑脚本
- 试着让 agent 生成一个验证脚本(比如 `python -c "..."` 测 SQL 注入)
- 确认宿主机没被污染

**完成标志**:所有代码执行都在沙箱里,宿主机只做调度。

**实际实现**:
- `SandboxSession` 封装,支持 sandbox(真实 OpenSandbox)和 local(本地模式,不用沙箱,原 "mock" 模式已重命名)两种模式
- 使用 OpenSandbox 官方 `SandboxSync` 同步 API,对齐 react_agent 的同步循环
- 支持 SSH key 挂载(私有仓库 clone)、资源限制(CPU/内存/执行时间)、端口转发
- 后台命令管理(ACP bridge 等长驻服务)
- **local 模式四层安全策略**(开发期使用,不能替代容器隔离):
  1. 路径策略:`.git` + 配置只读目录(`.vscode` / `.trae` / `.idea`)写保护(`check_local_write_permission`)
  2. 命令白名单:`_classify_command` 按 `SANDBOX_LOCAL_SAFE_COMMANDS` / `SANDBOX_LOCAL_DANGEROUS_COMMANDS` 分类 safe / normal / dangerous
  3. 危险命令前端确认:`command_confirm` SSE 事件 + `CommandConfirmDialog` 弹窗(显示完整命令 + 拦截原因)
  4. 平台原生隔离(可选):macOS `sandbox-exec` / Linux `bwrap`,Windows 跳过

---

## 阶段 3:加 Semgrep 和 CVE 查询 ✅

**目标**:工具集补全到 spec 里的完整版。

**做什么**:
- `semgrep_scan`:沙箱里预装 Semgrep,agent 调用跑扫描,结果解析回 Finding
- `cve_query`:解析依赖文件(requirements.txt / package.json / go.mod),查 OSV API
- 工具结果格式化:让 LLM 能读懂 Semgrep 和 CVE 的输出

**验证方式**:
- 找一个有已知漏洞依赖的仓库(比如故意 pin 一个老版本 `requests==2.20.0`),看 CVE 工具能不能查出来
- 找一个有典型漏洞模式的代码,看 Semgrep 能扫出来
- agent 能把 Semgrep / CVE 结果纳入最终报告

**完成标志**:react_agent 工具集达到 spec 8.4 的完整版。

**实际实现**:
- `run_semgrep` 工具:在沙箱中执行 Semgrep,支持自定义规则
- `query_cve` 工具:按依赖逐个查 OSV API
- 额外工具:`list_files`(目录结构)、`find_files`(glob 查找)、`write_file`(工作区写文件)、`run_python_code`(沙箱执行 Python)
- `list_skills` / `skill`:查看并加载专家技能

---

## 阶段 4:引入 agent2(检查助手),双智能体协作 ✅

**目标**:把 agent2(检查助手)加进来,跑通真正的双 agent 闭环。**这是核心创新,前面所有阶段都是为它做准备。**

**做什么**:
- agent2 模块:输入用户意图 + agent1 结果,输出评估 + 追问
- 漏洞类别 checklist 落地成结构化数据(不是 prompt 里的自然语言)
- 协作流程编排:
  1. agent2 意图对齐(可选提问)
  2. agent1 首轮扫描
  3. agent2 对照 checklist 评估
  4. agent2 生成定向追问 → agent1 续查
  5. 终止条件判定
- 对话记录全部写入 Conversation 表

**验证方式**:
- 提交一个复杂点的仓库(故意留多个类别的漏洞)
- 观察 Conversation 表,看到 agent2 真的在追问
- 对比:同一个仓库,单 react_agent(阶段 3)vs 双 agent,看双 agent 是否发现更多
- 终止条件触发后,checklist 所有类别都有结论

**完成标志**:agent2 真的会追问,且追问后 agent1 能补出新发现。

**实际实现(超出原规划)**:
- **场景降级**:checklist 不再固定,由 agent2 第 0 轮动态生成 + 用户编辑确认,后续轮按此评估
- **用户澄清(ask_user)**:第 0 轮初始评估时,agent2 可向用户提问(最多 2 轮),前端弹窗交互
- **跨轮记忆传递**:agent1 和 agent2 各自的三级压缩记忆策略
- **Plan 状态管理**:跨轮 plan 续接,避免重复规划已完成项
- **循环检测**:滑动窗口检测连续相同调用 + 交替循环,自动打破
- **任务暂停/恢复**:用户可暂停运行中的任务,后台线程在检查点阻塞
- **用户补充消息**:运行中注入 LLM 上下文 / 完成后触发新一轮协作
- **思考链流式推送**:reasoning_content 通过 SSE 实时推给前端

---

## 阶段 5:SKILL 机制 ✅

**目标**:把常用的多步审计操作封装成可复用技能,让 react_agent 调用。

**做什么**:
- SKILL 定义格式(YAML)与加载器
- SKILL 执行引擎:按 steps 顺序调底层工具,支持 iterate_over / condition
- 首版 7 个技能(check_sql_injection / check_auth_idor / check_deserialization / check_ssrf / check_hardcoded_secrets / check_path_traversal / check_crypto_misuse)
- react_agent 的工具列表里加入 `skill` 工具
- 管理员后台增删技能的接口

**验证方式**:
- 提交仓库,看 react_agent 是否主动调用 skill
- 故意破坏某个 skill 的 steps,看是否报错清晰
- 加一个新 skill(只改 YAML 不改代码),看是否能被加载

**完成标志**:技能可配置、可扩展,react_agent 能按需调用。

**实际实现(与原设计差异)**:
- SKILL 定义格式从 YAML 改为 **SKILL.md**(Markdown frontmatter + body),更接近主流 AI 工具的技能格式
- `list_skills` / `skill` 工具:react_agent 查看可用技能列表并按需加载技能指令
- 管理后台 CRUD API(`/skills`):创建/查看/更新/删除/重载技能
- **场景降级后**:skill 不再与场景绑定,而是全局可用。用户创建任务时可选择允许调用的 skill(`allowed_skills`)

---

## 阶段 6:用户系统与模型配置 ✅

**目标**:从单用户 demo 变成多用户可用。

**做什么**:
- 注册 / 登录 / 邮箱验证 / 重置密码(注意:禁止修改密码)
- JWT 鉴权,任务按 user_id 隔离
- 管理员后台:模型清单 CRUD + 默认模型配置
- 用户设置:选 agent2 / agent1 各自的模型
- 任务关联用户的模型选择

**验证方式**:
- 两个用户注册登录,各自的任务互不可见
- 用户 A 配置用 GPT-4,用户 B 配置用 DeepSeek,各自任务走对应模型
- 忘记密码流程跑通

**完成标志**:多用户隔离 + 模型可配。

**实际实现(超出原规划)**:
- 邮箱密码注册/登录 + 邮箱验证 + 重置密码 + **修改密码**(spec 原设计禁止修改密码,实际实现为允许,见 spec 8.5 更新说明)
- Git 平台 OAuth 登录(自动注册/关联已有账号):GitHub + Gitee 双平台,统一 `GitProvider` 抽象层
- Git 平台绑定:用于私有仓库 clone(repo / projects scope),同一用户可同时绑定 GitHub + Gitee
- 删除账号(硬删除 + 级联清理,含 UserGitBinding)
- 用户 LLM 配置(UserLLMConfig):列表式配置,每个配置含 provider/api_key/model/enable_thinking/base_url
- 用户 Agent 配置(UserAgentConfig):存储外部 CLI agent 的凭证(如 Qoder CLI PAT / DeepSeek API Key)
- 任务级模型选择:`llm_config_id`(agent2 用)+ `react_llm_config_id`(agent1 用,空时回退)
- 任务级执行器选择:`executor` 字段(builtin / qoder_cli / deepseek_cli / codex_cli)

---

## 阶段 7:网站前端 ✅

**目标**:Vue3 + TS 网站,能完整用图形界面跑双智能体任务。前端与场景解耦,同一套 UI 支持任意场景,不绑定安全审查。

**做什么**:
- 登录 / 注册 / 设置页(模型配置)
- 提交任务页:按场景声明动态渲染表单字段(场景自带字段定义,不写死 repo_url/branch)
- 任务列表侧栏(状态、当前阶段)
- 任务详情页:
  - 实时对话流(agent2 / agent1 的来回)
  - 结果清单:分组维度由场景声明(安全场景才用 severity),点击跳转源码位置
  - 覆盖度看板:评估维度由场景声明(安全场景才用 checklist),不写死
- 报告导出(Markdown / PDF)

**验证方式**:
- 从注册到跑完一个任务,全程不用碰命令行
- 任务运行中能看到 agent 在做什么
- 同一套界面换一个场景,结果清单和看板也能正常展示
- 报告能导出

**完成标志**:网站端完整可用,能替代 curl 流程,且不耦合具体场景。

**实际实现(超出原规划)**:
- Vue3 + TypeScript + Vue Router + Pinia
- 完整页面:LoginView / VerifyEmailView / ResetPasswordView / OAuthCallbackView / HomeView(任务列表)/ TaskCreateView / TaskDetailView / ModelSettingsView / SettingsView / SkillManagementView / MemoryManagementView / CollaborationPolicyView
- 实时对话流:ConversationMessage 组件,支持 thinking / tool_call / tool_result / evaluation / question / summary / verify 等消息类型
- 流式思考链:thinking_delta SSE 事件,打字机效果实时展示 reasoning + content
- 覆盖度看板:ChecklistReviewDialog(第 0 轮用户编辑确认清单)+ 任务详情页覆盖度展示
- 工作区侧栏:WorkspaceSidebar 组件,浏览 clone 的文件结构和内容
- 暂停/恢复按钮 + 用户消息输入框(UserMessageInput)
- 报告导出:Markdown 下载 + HTML 打印为 PDF
- 模型配置:ModelConfigDialog + ModelCombobox + AgentConfigDialog
- Git 平台绑定:GitProviderDialog(统一 GitHub / Gitee,按 provider 动态渲染标题/品牌色/scope 文案),解绑时后端主动撤销平台 Token
- 多平台仓库选择:TaskCreateView 并行加载已绑定平台仓库,合并到带 `[GitHub]`/`[Gitee]` 标记的统一下拉
- 登录页:GitHub + Gitee 双 OAuth 登录按钮
- 全文搜索:任务列表支持按标题/输入/对话内容/结果内容搜索
- **新手引导(OnboardingTour)**:首次登录播放分步引导,按路由分组,`data-onboarding` 属性匹配锚点,版本号递增使老用户重新看到
- **主题切换**:浅色 / 深色 / 跟随系统三档,顶栏主题切换按钮
- **帮助文档弹窗**:顶栏问号按钮打开 help.md 渲染的帮助弹窗
- **账号设置入口收敛**:账号设置只从顶栏齿轮按钮进入,不在主导航
- **任务运行中弹窗**:QuestionDialog(澄清提问)/ ChecklistReviewDialog(checklist 确认)/ VerifyActionDialog(verifier 授权)/ CommandConfirmDialog(local 模式危险命令 + sandbox 模式内置 react_agent `per_command` + CLI `per_command` 命令确认)
- **技能管理页**:SkillManagementView,用户上传 / 编辑自定义 skill(SKILL.md),隔离存储
- **记忆管理页**:MemoryManagementView,管理用户偏好 / 全局记忆 / 项目记忆三类长期记忆
- **智能体策略页**:AgentPolicyPanel,配置评估频率 + 验证权限 + verifier 测试环境 + 登录 token + AI助手命令确认模式

---

## 阶段 8:微信小程序 ⬜

**目标**:小程序端,核心功能可用。延续阶段 7 的场景无关方向,小程序 UI 同样不绑定安全审查。

**做什么**:
- 微信登录(复用网站账户体系)
- 提交任务:按场景声明动态渲染表单(不写死 repo_url)
- 任务列表 + 状态轮询(5-10 秒)
- 任务详情:精简版结果卡片(分组维度由场景声明)
- 设置页:模型配置
- 跳转网页版看完整报告

**验证方式**:
- 手机上提交任务,过几分钟回来看结果
- running 状态下能看到当前阶段
- 结果卡片能看清重点
- 换一个场景,小程序表单和结果卡片也能正常展示

**完成标志**:小程序端能完整走一遍提交 → 等待 → 查看结果,且不耦合具体场景。

---

## 阶段 9:生产化与上线 🔨

**目标**:能真上线给别人用。

**做什么**:
- Postgres 替换 SQLite
- 任务队列(Celery / RQ),任务异步执行
- 部署:Docker Compose(后端 + 数据库 + 沙箱)
- 资源限制(单任务 token / 时间上限)
- 监控与日志
- 微信内容安全检测(避免报告被拦截)
- 域名 + HTTPS + 备案

**验证方式**:
- 线上环境部署完成,真实用户能注册使用
- 压测:并发任务不崩
- 跑一个真实开源项目,看完整任务效果(结果质量因场景而异,不预设安全审查)

**完成标志**:线上可访问,真实仓库任务可用。

**当前进度**:
- ✅ 数据库已支持 PostgreSQL(SQLAlchemy + PostgreSQL 方言,JSONB/UUID)
- ✅ 任务异步执行(后台线程 threading,暂未用 Celery/RQ)
- ✅ 沙箱部署脚本(scripts/build-sandbox-image.sh)
- ✅ 资源限制(沙箱 CPU/内存/执行时间限制;react_agent MAX_ITERATIONS + 循环检测)
- ✅ 沙箱 TTL 自动续期(访问路径探活顺带续期;CLI 长阻塞段走 `SANDBOX_RENEW_INTERVAL_MINUTES`
  的 `auto_renew`)+ 克隆深度/超时可配(REPO_CLONE_DEPTH / REPO_CLONE_TIMEOUT)
- ✅ 沙箱实例回收后的自适应:过期被归一为 `SandboxGoneError` → 丢弃会话 + 回 410 +
  引导重新克隆,复用会话时发现已回收则重建容器(避免"看着可用、什么也读不到")
- ✅ LLM 429 限流退避重试(LLM_RATE_LIMIT_MAX_RETRIES)+ CLI 挂死兜底(ACP_IDLE_TIMEOUT_*)
- ⬜ 任务队列(Celery/RQ):当前用 threading,生产环境需切换
- ⬜ 监控与日志系统
- ⬜ 微信内容安全检测
- ⬜ 域名 + HTTPS + 备案

---

## 阶段 10:练习题与自适应练习 ✅

**目标**:把「审计任务产出」与「学习练习」打通——任务真实发现一键转题库,按遗忘曲线 + 薄弱点自适应组卷,把分析平台变成学习平台。

**做什么**:
- 题目生成(generator.py):任务 Results 逐条调 LLM 生成客观题(单选/判断),严格 JSON + 重试 + 字段校验 + 去重,草稿确认制
- SM-2 遗忘曲线(sm2.py)+ 难度评估与能力估计(difficulty.py)
- 综合选题(selector.py):到期复习优先 > 薄弱点强化 > 难度匹配 > 新知识引入
- 四主题提示词(网络安全 / 架构设计 / 通用代码能力 / 合同文书,出题时自动匹配)+ 材料注入 + 迷你工具循环 + 工作区恢复
- 出题模型三级解析(task > 用户级默认 > env)+ 思考模式三态覆盖
- 异步出题 job + SSE 进度 + 出题日志落盘;任务完成自动出题(auto_generate)
- 前端 PracticeView(练习 / 统计 / 题库管理)+ 出题进度侧栏 + 设置弹窗;`PRACTICE_ENABLED` 开关

**验证方式**:
- 跑一个任务 → 生成练习题 → 预览确认 → 练习会话作答 → 看对错解析与知识点掌握度变化
- 连续作答观察 SM-2 间隔与选题权重变化
- 开关 PRACTICE_ENABLED 验证前后端入口隐藏

**完成标志**:任务 → 题库 → 自适应练习的完整闭环可用。

---

## 阶段 11:工作区变更 ✅

**目标**:把任务的工作区产物留存下来(本阶段曾实现的检查点评估/软中断因价值/成本比不高,后续已整体移除,agent2 保留 round 边界完整评估)。

**做什么**:
- AgentPolicy 独立表:用户级默认从 user_preferences JSONB 迁移,任务级可覆盖
- 工作区变更捕获(workspace_diff):任务完成时合成 git patch 存 task_artifacts,前端只读展示
- 代码审查能力增强:run_lint / run_coverage / list_dependencies 工具 + 3 个 code_review skill

**验证方式**:
- 任务完成后查看工作区变更区 diff 展示

**完成标志**:工作区产物留存闭环可用。

---

## vibe coding 的几个原则

1. **每个阶段做完先玩**。别急着进下一阶段,先用真实仓库跑几次,发现问题再改。
2. **阶段 4 是关键转折**。前面都是铺垫,阶段 4 才是核心创新落地。如果阶段 4 做出来效果不好(双 agent 没比单 agent 强),停下来想原因,不要硬往后做。
3. **允许回退**。某个阶段发现前面设计有问题,回去改前面,不要将就。
4. **每个阶段都要能 demo**。不能 demo 的阶段就是没做完。
5. **工具优先于 prompt**。react_agent 找不到漏洞,先想是不是工具不够用,而不是先调 prompt。

---

## 不在路线图里但要做的事(穿插进行)

- 安全:输入校验、SSRF 防护(repo_url 不能是内网地址)、rate limit
- 成本:每个任务的 token 消耗统计与展示
- 错误处理:LLM 调用失败、沙箱超时、仓库不存在等
- 可观测性:每个 agent 的执行日志、工具调用耗时
