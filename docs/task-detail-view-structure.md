# 任务详情界面(TaskDetailView)显示结构

> 描述对象:`frontend/src/views/TaskDetailView.vue` —— 双 Agent 协作的核心可视化界面。
> 本文档描述**迭代摘要行("N 个工具调用: xxx")及其外框移除后**、
> **右侧栏布局调整(状态/导出迁入标题行、结果置底、移除用户意图卡片)后**的目标结构。

## 1. 页面整体:三栏布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ AppHeader(顶栏,左侧含历史任务栏折叠按钮)                              │
├────────────┬──────────────────────────────────┬───────────────────────┤
│ 左侧        │ 主区 main                        │ 右侧 detail-sidebar    │
│ Workspace  │ (协作对话流,页面核心)             │ (核查与结果抽屉)         │
│ Sidebar    │                                  │                       │
│ 历史任务列表 │                                  │ 概览/任务清单/验证/     │
│ (可折叠隐藏) │                                  │ 核查/结果(可折叠)       │
└────────────┴──────────────────────────────────┴───────────────────────┘
```

- **左侧 WorkspaceSidebar**:历史任务列表,支持删除/重命名;折叠时完全隐藏,由顶栏按钮切换。
- **右侧 detail-sidebar**:默认宽度 420px,可经左缘手柄拖拽调整(320px ~ min(640px, 50vw)),
  按「用户 email + 栏位」记忆到 localStorage;默认展开,折叠后退化为右上角悬浮把手
  (有结果时显示数量角标)。详见 §5。

## 2. 主区 main 纵向结构

```
main
├── conv-header          标题/状态行(固定,不随对话滚动)
├── main-scroll          滚动容器
│   ├── conversation-section   协作对话流(核心,见 §3)
│   └── workspace-changes-section  工作区变更(git diff,仅有产物时)
└── UserMessageInput     用户补充消息输入框(running/paused/completed 可见)
```

### 2.1 conv-header(标题/状态行)

- 左:任务标题(截断显示)+ 创建时间。
- 右:运行态显示红色"实时"徽标 + 暂停按钮;暂停态显示橙色"已暂停"徽标 + 恢复按钮。

### 2.2 工作区变更区

任务完成时捕获的 git diff patch,只读展示(按行着色);头部显示变更文件数/字符数/截断提示,支持折叠。

## 3. 协作对话流(conversation-section)

对话流按 `round_idx` 分组,层级如下:

```
conversation-section
├── user-directive                 用户指令(右对齐气泡,从对话中提取,置顶)
└── round-group × N                轮次分组(不显示轮次标签,"轮"概念不暴露给用户)
    └── messages                   消息容器(flex 纵向,gap 控制间距)
        ├── plain segment × N      平铺段(关键消息,单张卡片直接显示)
        ├── step group × N         有 plan:每个 step 一个折叠组;无 plan:单个"执行过程"折叠组
        └── conclusion segment     该轮最终回答(正常消息样式,不折叠直接可见;每轮至多 1 个)
```

> 计划清单已移至右侧栏"任务清单"(见 §5.2),主对话流只保留消息流。
> 结构取向:过程(思考+工具)整体收进折叠组,最终回答像正常聊天消息一样组外直出
> ——类似把 ChatGPT 的"思考块"扩大到包含工具调用。展开由**生命周期**驱动而不是
> 由点击驱动:运行中的活跃轮(过程组的前沿组 + 组内正在流动的思考卡)自动展开,
> 轮结束/任务完成后自动收起,兼顾"看直播"与"读干净的聊天记录"。

### 3.1 plain segment(平铺段)

不进折叠块、直接渲染为单张卡片的关键消息:

- **agent2 真追问**(修正指令卡):主对话流**唯一保留**的检查助手内容,
  包 warning 色系 `.followup-card`("⚠ 检查助手修正指令");判定函数
  `isAgent2Followup`(role=agent2 && type=evaluation && content 非空且
  不以三个非追问标记开头,与后端 `_is_ua_followup_evaluation` 逐字对齐);
- 用户补充消息(type=message,右对齐,与顶部 userDirective 视觉一致);
- 其余 agent2 输出(思考/工具核查/评估完成/总结)一律过滤出主对话流
  (roundGroups 在 localIdx 计数后跳过),由右侧栏 Agent2Panel 展示。

### 3.2 step group(步骤分组,折叠块)

**唯一的折叠单位**,整体收起、按需展开:

- 有 plan 时:归属同一 plan step 的若干迭代合并为一个折叠组(文字 = step 文本);
- 无 plan 时:该轮所有迭代归入单个 **"执行过程"** 折叠组(结论已提出组外,组内是纯过程);
- 无法归属 plan step 的迭代也归入 "执行过程" 组。

```
step-block
├── step-header(点击切换折叠)
│   ├── 折叠箭头 ▶/▼
│   ├── 状态图标(✓ done / ◌ in_progress / ○ pending;无状态组不显示)
│   ├── step 文本
│   └── 打字动画(含流式内容时)
└── step-body(展开时)
    └── 迭代内容 × N(直接平铺,无摘要行、无边框包装)
        ├── thinking 卡片          ConversationMessage(流式或历史)
        ├── 工具渲染行 × N          见 §3.3
        └── otherItems 卡片        submit 等其他项
```

**折叠策略**(`isStepExpanded`):

1. 用户手动收起优先级最高(`collapsedSteps`);
2. 用户手动展开、或是**活跃轮的前沿组**(最新迭代所属组;整轮运行期间恒展开,
   工具执行的空档也不收——此前只看 `hasStreaming`,thinking 一收完转去跑工具就
   收起、下一次 thinking 到达再展开,直播在迭代边界上反复断流)、
   或组内含流式中内容、或组内有用户消息(追问/回答)→ 展开;
3. 其余默认折叠(过程是噪音,结论由结论段承担,见 §3.4)。

**思考卡展开策略**(`utils/thinkingExpand`,主对话流与右侧栏 Agent2Panel 共用):

思考链(reasoning)同属"运行中的直播内容",不再默认折叠等用户点:

| 阶段 | 展开状态 | 说明 |
|---|---|---|
| 流式中 | reasoning 累积过 `AUTO_EXPAND_MIN_CHARS`(80 字)后展开 | 阈值之下的短思考保持单行标题,避免一次任务几十个迭代各自闪一下 |
| 结束后 | 宽限期(`COLLAPSE_GRACE_MS`,400ms)内保持展开,到期折叠 | 只给"曾自动展开过"的卡片;让收起不与正文出现抢同一帧 |
| 用户点过 | `reasoning_pin` 覆盖以上两条 | 流式中手动收起不会被下一个增量重新展开;结束后手动留着也不被吞 |

配套约定(都是"展开之后才暴露"的问题,缺一个就等于没展开):

- 思考框 300px 封顶带内部滚动,流式期间必须贴住底部,否则用户看到的是思考开头、
  最新 token 落在可视区外(`ConversationMessage` 的 `innerFollow`);
- 流式期间用纯文本直出(`.msg-reasoning-live`),结束后才一次性渲染 markdown——
  思考链可达数千字符,逐 token 重解析会把渲染成本扣在每个增量上;
- 收起/展开带 0.18s 过渡(`.thinking-box`),不把下方消息猛地拽上去;
- 子智能体"内部思考"(`${callId}-think`)与工具参数/结果**不参与自动展开**:它们嵌在
  已折叠的工具卡里,自动展开要连带撑开父卡,层级与面积都失控。

### 3.3 迭代与工具渲染行

一个**迭代** = agent1 一次 ReAct 循环(thinking + N 个 tool_call/tool_result)。
切分规则:遇到 agent1 thinking 开新迭代,后续 agent1 工具项归入当前迭代,
遇到下一个 thinking 或非 agent1 消息则关闭当前迭代。

**锚点缺失兜底**:thinking 锚点可能缺失(空 thinking 未落库、CLI agent 未发文本
直接发起工具调用、前面的非 agent1 消息关闭了迭代)。此时开一个无 thinking 的
兜底迭代承接工具项,保证其仍归入 step 组正常渲染,而不是退化为 plain 段
被追加到"执行过程"折叠块末尾。

迭代内容在 step-body 内**直接平铺**(不再包 "N 个工具调用: xxx" 摘要行和外框),
工具项经 `toolRowsOf` 配对后渲染为四种行类型:

| 类型 | 触发条件 | 形态 |
|---|---|---|
| `compact` | 浏览型工具(read_file/list_files/search_code 等) | 单行摘要(🔧 意图),点击轻量展开原始结果 |
| `agent` | 子智能体调用(`[Agent]`) | 标题卡片,展开后:子任务参数 + 内部思考(二级折叠)+ Markdown 报告 |
| `toolpair` | 普通工具 | 标题卡片,展开后:调用参数 + 工具结果(等宽块) |
| `plain` | 落单项(如孤儿 tool_result) | ConversationMessage 原样渲染 |

工具行默认折叠,展开状态按 tool_call id 记录(`expandedToolRows`,
子智能体内部思考用 `${callId}-think` 复合键)。

### 3.4 conclusion segment(结论段)

该轮 agent1 的最终回答,**从最后一个 step 组中提出、组外平铺直接可见**,是主对话流里
用户回看时的视觉焦点:

- **来源**:agent1 每轮总结 = 该轮最后一条 thinking 的 content(ReAct 循环在无工具
  调用时结束,见后端 `react_agent`);即最后一个"纯思考"迭代(有 thinking、无
  toolItems/otherItems、content 非空);
- **提取时机(轮闭合判定)**:该轮已有 agent2 活动(思考/评估在 agent1 该轮结束后
  才开始记录)、或任务不在运行中、或不是最后一轮。运行中的末轮不提取——新迭代开头
  也是纯思考,提前提取会造成结论闪现再跳回过程组;
- **位置**:所有 step 组之后、轮末平铺消息(如修正指令卡)之前;
- **渲染**:纯正文消息,**无特殊标签、无思考卡**(与主流一致:最终回答就是一条
  普通消息);最终思考的 reasoning **留在过程组内**(组内该思考项只显示思考卡,
  正文卡被剥离,`stripItemContent` / `stripItemReasoning` 克隆时加 `-think`/`-body`
  后缀保证模板 key 唯一);无思考可留时整迭代提出(`conclusionPopped`,
  plain 定位需补虚拟槽位);
- **plain 定位补偿**:整迭代提出时用"虚拟迭代数"保持 plain 消息的原有时间顺序——
  结论前的 plain 仍落在过程组与结论之间,结论后的(评估/修正指令)仍落轮末。

### 3.5 运行中等待提示(waiting-hint)

任务运行中且无流式项时显示:优先展示后端推送的克隆进度
(阶段文案 + 百分比 + 进度条),否则显示通用打字动画;暂停态不显示动画。

## 4. 数据组装管线

```
task.conversations(正式对话,含历史 thinking 还原)
  + streamingItems(实时流式 thinking,SSE 推送,不入 convs)
      │  按 round_idx 归组;seq 稳定排序
      │  (正式对话 seq = 轮内下标×1000;流式项 seq = insertSeq×1000−500,
      │   保证 thinking 恰好插在其后 tool_call 之前)
      ▼
roundGroups(computed)
      │  轮闭合判定:该轮有 agent2 活动 / 任务不在运行中 / 非末轮
      │  每轮:segmentRoundItems()
      │    一阶段:按 thinking 切迭代,非 agent1 消息记为 plain 段
      │    一阶段半:轮闭合时,最后一个纯思考迭代提为 conclusion 段
      │    二阶段:迭代按 plan step 关键词推断归组(TOOL_STEP_KEYWORDS),
      │           无 plan / 无法归属 → "执行过程"折叠组;
      │           plain 段按轮内原始位置穿插到组间/组内迭代边界
      │           (结论迭代占虚拟槽位,保持结论前后 plain 的时间顺序)
      ▼
RoundGroup { roundIdx, segments, planSteps }
```

## 5. 右侧栏 detail-sidebar

### 5.1 标题行(detail-sidebar-header)

```
┌──────────────────────────────────────────────┐
│ 核查与结果        [状态徽标] [⬇下载] [🖨打印] [⟩⟩] │
└──────────────────────────────────────────────┘
```

- 左:标题"核查与结果";
- 右(自左至右):**状态徽标**(如"已完成",随 task.status 实时变化)→
  **下载按钮**(导出 Markdown 报告)→ **打印按钮**(打印/另存 PDF)→
  **侧栏折叠按钮**(WorkspaceToggleButton);
- 下载/打印按钮的显示条件保持原逻辑:任务 completed 或有结果时才出现;
  状态徽标始终显示(按钮隐藏时仅剩徽标)。

### 5.2 内容区(detail-sidebar-body)自上而下

1. **任务概览**(条件渲染:`(isRunning && current_stage) || error_message`):
   仅活跃期(pending/running/paused)显示"当前阶段",失败任务显示错误信息;
   任务进入终态且无错误时整块收起。场景/创建时间/完成时间已移除——
   创建时间在主区标题行,完成时间改为悬浮标题行状态徽标(title)查看。
   不再包含:状态徽标与下载/打印按钮(已移至标题行,见 §5.1)、
   用户意图卡片(不再显示;用户指令仍保留在对话流顶部 userDirective 气泡)。
2. **任务清单**(有 plan 时):原主对话流"计划清单"卡迁入,展示最新一轮的
   计划步骤(✓ done / ◌ in_progress / ○ pending)与进度 x/y;数据来自
   SSE plan 事件与历史对话提取(`<plan>` 块 / TodoList tool_call),
   随运行实时更新(`latestPlanSteps`)。
3. **动态验证**(配置了测试环境 URL 时):开关、授权模式切换、登录凭证(脱敏);不出现 verifier_agent 字样。
4. **检查助手核查**(Agent2Panel,有 agent2 活动时):agent2 的全部过程输出——
   按轮折叠组(进行中轮自动展开),轮组标题为核查摘要文案(如"3 次核查 · 1 条
   修正指令",不显示轮次数字),**轮内是一条时序穿插的条目流**(思考 → 它触发的
   工具 → 下一段思考 …),由 `utils/agent2Timeline.buildAgent2Rounds` 算定:
   含流式思考(SSE thinking_delta,verify 标记显示"动态验证";展开规则与主对话流
   思考卡同一套生命周期,见 §3.2 ——审查动辄数分钟且正文全在 reasoning,折叠着就
   等于只看得到字数跑)、历史思考链、工具核查(读码/PoC/引用复核,tool_call 与
   tool_result 配对为单行摘要+展开)、评估结论、最终总结卡。
   条目顺序与主对话流同一口径(落库项 seq=轮内下标×1000,实时卡片
   seq=insertSeq×1000-500),**不按 type 分桶**——分桶会把整轮思考堆到工具之后、
   并把实时卡片压在历史思考之上(读起来像倒叙),切断"这段思考引发了哪步核查"的
   因果链;tool_call/tool_result 配对复用 `buildToolSegments`(按 tool_call_id
   精确配对,并行调用错开落库也不散位,孤儿 result 走兜底行)。
   标题行右侧状态 badge 取 review_status(检查中 / 检查完成 / 检查失败 /
   检查已终止);**审查中额外提供一个「终止检查」按钮**(POST /tasks/{id}/review/stop,
   `stoppingReview` 置灰防重复点):后端是协作式取消(LLM 流 chunk 边界 / 工具
   循环边界生效),终态随 review_done=stopped 事件送达,本视图不轮询等终态
   (仅当后端告知"已无审查线程在跑"时拉一次快照兜底,防 SSE 已断时卡在"检查中")。
5. **重点与知识点**(最底部,原"结果清单"):agent2 done=true 提炼的
   3-8 条精选知识点(老任务为全量发现,兼容);含 `learning_note` 的卡片
   带"值得学"徽标,展开时正文上方显示学习点引用块;按 `task.params._grouping`
   动态分组(如按严重度,老任务);文件类 meta 标签可点击打开左侧工作区文件。
   标题行的临时结果提示随 review_status 分流:running →「检查助手整理中」
   (呼吸点,完成后自动更新),stopped →「检查已终止」(静止中性点:临时结果
   就是最终结果,不再等知识点替换;failed 不在这里另开提示,标题行 badge「检查失败」已说明)。

> 历史说明:覆盖度看板(task.checklist 驱动的维度卡片网格)已随覆盖度清单功能移除。

### 5.3 左缘调宽手柄(detail-resize-handle)

侧栏与主区同底色,原先没有分隔线也看不出边界,故:

- **常显边界**:`.detail-sidebar` 补 `border-left`(与左侧历史任务栏对称);
- **悬停可拖暗示**:左缘 6px 透明热区(`left:-3px`,压住分隔线两侧),
  hover / focus-visible 染 `--color-primary-light`,`cursor: col-resize`;
- **交互**:按住拖手柄往左 = 变宽(栏停靠右缘,方向取反);双击复位 420px ——
  **不靠原生 `dblclick`**(手柄 `pointerdown` 里 `preventDefault` 后,部分浏览器不再补发
  click/dblclick),改由 composable 在 `pointerup` 用「按下没移动 + 距上次点击 < 300ms」判定;
  键盘 ← / → 微调 24px,Home 复位;
- **无障碍**:手柄是可聚焦 `role="separator"`(WAI-ARIA Window Splitter),暴露
  `aria-valuenow / aria-valuemin / aria-valuemax`(valuemax 用当前视口下的**实际上限**,
  不是静态 max,免得把读屏用户引去拖到钳位外);
- **拖拽期间的全局态**:composable 给 `document.body` 挂 `is-resizing-sidebar` 类
  (`user-select:none`)+ 追加一层全屏透明遮罩 `.sidebar-resize-overlay`(`cursor:col-resize`)
  —— 右栏往左拖时指针会移进主对话流,只禁侧栏会在主区把正文选蓝、光标也会断,遮罩顺带
  挡掉源码查阅栏里 CodeMirror 抢指针;松手 / 卸载即撤;
- **约束**:`min 320 / max 640`,并再按 `50vw` 收一道上限 —— 右栏每变宽 1px
  都是从主对话流身上扣的;视口变窄时按新上限重钳,不保留顶破上限的宽度(不写盘,
  刷新从存档还原,故只丢本次会话的手感);
- **窄屏**:≤1024px 侧栏改为覆盖式抽屉(§5 顶部那条 @media),宽度归 CSS,
  手柄隐藏不响应拖拽 —— 断点取值必须与 `useResizableSidebar({ narrowMax: 1024 })` 一致;
- **记忆**:松手时写 `localStorage` 键 `secondlook:sidebar-width:task-detail:{email}`
  (拖拽过程中不写);用户未就位时读写都跳过(路由守卫已 `await fetchMe`,进入本受保护页时
  `auth.user` 必已就位,故挂载即生效)。

实现集中在 `frontend/src/composables/useResizableSidebar.ts`(指针跟手 / 钳位 /
窄屏判定 / 可选持久化),`PracticeCodeSidebar`(做题页源码查阅栏,默认 760px、
断点 640px、**不持久化**)复用同一套,两处不再各写一份拖拽代码。

## 6. 全局弹窗

| 弹窗 | 触发 |
|---|---|
| VerifyActionDialog | 动态验证 per_action 模式,逐动作授权 |
| CommandConfirmDialog | local 模式危险命令确认 |

## 7. 与旧版结构的差异(本次改动)

| 旧结构 | 新结构 |
|---|---|
| step group → iteration-block(边框+背景盒子)→ iteration-divider("N 个工具调用: xxx"摘要行)→ iteration-body | step group → 迭代内容直接平铺(wrapper 退化为透明容器) |
| 流式迭代靠盒子光晕(iteration-streaming)提示 | 由 step-header 打字动画 + 流式 thinking 卡片自身样式表达 |
| `iterationSummary()` / `toolCallCount()` 生成摘要文本 | 两个函数移除(信息已由工具行自身的单行摘要/卡片标题覆盖) |

保留不变:迭代切分逻辑(`segmentRoundItems`)、
step 组折叠策略、工具行四种渲染类型。

## 8. 右侧栏布局调整(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 标题行仅"任务详情" + 折叠按钮 | 标题行追加:状态徽标 + 下载按钮 + 打印按钮,排在折叠按钮左侧 |
| body 顺序:任务概览 → 动态验证 → 结果清单 | body 顺序:任务概览 → 动态验证 → 结果清单(本次改动只调控件位置;现行完整顺序已进一步演进为“任务概览 → 任务清单 → 动态验证 → 检查助手核查 → 重点与知识点”,以 §5.2 为准) |
| 状态徽标与下载/打印按钮在"任务概览"区块顶部(overview-header) | 迁入标题行;概览区块后又精简为仅活跃期"当前阶段"+错误信息(场景/创建/完成时间移除,见 §5.2) |
| 任务概览含用户意图卡片(user_input 的 Markdown 渲染) | 移除,不再显示(对话流顶部 userDirective 气泡仍保留用户指令) |

保留不变:下载/打印按钮的显示条件(completed 或有结果)、导出逻辑
(exportMarkdown / exportPdf)、结果卡片折叠交互。

> 本节为当时那次改动的对照表。“覆盖度”区块在更早的“覆盖度清单”功能移除时就已经
> 不存在了(见 §5.2 历史说明),旧表两列均不应含它,已更正。

实现要点(供代码改动参考):

1. `detail-sidebar-header` 内新增状态徽标 + `overview-actions`(下载/打印),
   位于 WorkspaceToggleButton 之前;原 overview-section 的 `overview-header` 整块移除。
2. body 内把 `sidebar-results` section 移到 `verifier-section` 之后。
3. 移除 overview-section 内的 `.overview-input`(用户意图)块;相关 CSS 一并清理。
4. 标题行空间有限:状态徽标与按钮需紧凑样式(小尺寸图标按钮),
   避免挤压标题;窄屏下优先保标题截断而非换行。

## 9. 过程整体收起 + 结论直出(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 每轮最终总结埋在 step 组(执行过程)内,完成后折叠,要点开才能看 | 最终回答提为 conclusion 段,组外以正常消息样式直接可见(见 §3.4) |
| 任务完成后最后一组 step 默认展开(兜底露出最终总结) | 规则移除:step 折叠组一律默认折叠(运行中流式自动展开),结论由结论段承担 |
| 结论卡带"本轮结论"胶囊标签;思考灰卡挂在结论消息上方 | 标签与思考卡均移除:最终思考留在"执行过程"组内(组内只显示思考卡),结论为纯正文消息 |
| step 头部"N 次迭代"计数徽标、无状态组的"·"占位点 | 移除:头部只留折叠箭头 / 状态图标(有 plan)/ step 文本 / 流式动画 |
| 无 plan 时所有迭代包进单个"执行过程"折叠块(总结也藏在里面) | "执行过程"折叠组保留(过程整体收起),但总结已提出组外,组内是纯过程噪音 |
| 主对话流每轮顶部显示"计划清单"卡 | 移至右侧栏"任务清单"(最新一轮计划+实时进度,见 §5.2.2) |
| 主对话流每轮显示轮次标签("初始评估"/"第 N 轮") | 移除,"轮"概念不暴露给用户(轮边界由修正指令卡自然标示) |
| 重点与知识点卡片带"第 N 轮"徽标 | 移除徽标 |
| Agent2Panel 轮组标题"第 N 轮核查" + 右侧摘要 | 标题即摘要文案("核查中…"/"3 次核查 · 1 条修正指令 · 已完成"/"核查完成") |

保留不变:迭代切分逻辑、工具行四种渲染类型、step 折叠组的手动展开/收起状态、
修正指令卡判定(`isAgent2Followup`)、Agent2Panel 轮组折叠结构(仅换标题)。

实现要点(供代码改动参考):

1. `segmentRoundItems` 增加 `roundClosed` 参数,一阶段半提取结论;
   提取后 plain 定位用"虚拟迭代数"补偿(见 §3.4)。
2. 轮闭合判定在 `roundGroups`:该轮有 agent2 活动(`agent2Rounds`)/
   任务非运行中(`!isRunning`)/非末轮(`roundIdx !== lastRoundIdx`)。
3. 结论段复用 ConversationMessage 正文卡,无包裹无标签;最终思考留在过程组内
   (`itemFieldText` / `stripItemContent` / `stripItemReasoning` 做流式与正式项的
   形态适配,克隆加 `-think`/`-body` 后缀保证 key 唯一,展开状态按 conv_id 共享)。
4. 侧栏"任务清单"用 `latestPlanSteps` computed:遍历 `planPerRound`(reactive Map,
   SSE plan 事件与 `extractPlanFromHistory` 双来源写入)取有 plan 的最大 round。
