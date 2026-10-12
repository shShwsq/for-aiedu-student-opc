# 任务详情界面(TaskDetailView)显示结构

> 描述对象:`frontend/src/views/TaskDetailView.vue` —— 「AI助手 执行 + 检查助手 后台质检」流水线的核心可视化界面。
> 本文档描述**迭代摘要行("N 个工具调用: xxx")及其外框移除后**、
> **右侧栏改为常驻竖向分段 rail + 按需展开面板(原「结果」段拆成「审查结果」与
> 「重点与知识点」两段,建议追问方向归入「概览」)后**的目标结构。

## 1. 页面整体:三栏布局(右侧为常驻 rail + 按需展开面板)

```
┌──────────────────────────────────────────────────────────┐
│ AppHeader(顶栏,左侧含历史任务栏折叠按钮)                       │
├───────────┬────────────────────────────┬─────────┬────────┤
│ 左侧       │ 主区 main                  │ 展开面板   │ rail   │
│ Workspace │ (协作对话流,页面核心)        │ (按需渲染) │ (常驻)  │
│ Sidebar   │                            │ 当前段内容  │ 4 段图标│
│ 历史任务列表│                            │          │ +状态   │
│ (可折叠隐藏)│                            │          │ +下载打印│
└───────────┴────────────────────────────┴─────────┴────────┘
```

- **左侧 WorkspaceSidebar**:历史任务列表,支持删除/重命名;折叠时完全隐藏,由顶栏按钮切换。
  切到某任务的工作区并选中文件时,右侧会滑出「文件查看面板」(`.file-panel`,默认 480px,
  `.workspace-sidebar` 固定 280px 在其左):可经其**右缘手柄**拖拽调宽(360px ~ min(720px, 50vw),
  ≤640px 覆盖抽屉态隐藏手柄),双击复位,同样复用 `useResizableSidebar`(`side:'left'`,方向相对右栏
  镜像),按「用户 email + 槽位」记忆到 `secondlook:sidebar-width:file-panel:{email}`。
- **右侧 rail + 展开面板**:rail 常驻页面右缘(56px),是**唯一的展开与收起入口**——收起态
  没有独立把手,展开面板标题行也没有收起按钮,也没有折叠态悬浮角标(原有两套展开入口与
  tab 行一并收敛成 rail)。
  展开面板默认宽度 420px,可经其左缘手柄拖拽调整(320px ~ min(640px, 50vw)),
  按「用户 email + 栏位」记忆到 localStorage;当前展开段按「用户 email」记忆到
  `secondlook:sidebar-section:task-detail:{email}`。详见 §5。

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

> 计划清单已移至右侧栏"任务清单"(见 §5.3),主对话流只保留消息流。
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

## 5. 右侧栏:常驻 rail + 按需展开面板

右栏状态只有**一个** `activeSection: 'overview' | 'process' | 'findings' | 'knowledge' | null`
(`null` = 收起为 rail)。原先「顶部横排 tab + 独立折叠开关 + 折叠态悬浮把手」
三套展开入口收敛为 rail 上的四个 disclosure 按钮,因此不再用 `role="tablist"/tab/tabpanel`
语义:按钮带 `aria-expanded` + `aria-controls="detail-panel"`,面板是带 `role="region"`
+ `aria-label`(取当前段名)的 `#detail-panel`。

### 5.1 分段 rail(detail-rail,常驻 56px)

```
┌─────┐
│ (i) 3│  概览       角标 = 两组建议追问条数(缺口 + 探索)
│ (⌕) ●│  核查过程   呼吸点 = review_status running
│ (⛨) 5│  审查结果   角标 = reviewItems.length
│ (▤) 8│  重点与知识点 角标 = task.results.length
│ 已完成 │  状态徽标(悬浮 title 显示完成时间)
│ [⬇]  │  下载 Markdown
│ [🖨] │  打印 / 另存 PDF
└─────┘
```

- 只有图标,`title`/`aria-label` 载段名(56px 宽摆不下「重点与知识点」这类长标签),
  展开态额外带「（再次点击收起）」——收起没有别的显式控件了,隐式 toggle 不写出来就没人发现;
  展开后由面板标题行的段名交代在看哪一段(见 §5.2);
- 角标超 99 显示 `99+`;点当前已展开段的按钮 = 收起;
- **审查状态变化不自动弹栏**,只点亮对应按钮的提示环(`has-attention`)+ 角标:
  running → 核查过程,done/failed/stopped → 审查结果,单 agent 模式完成且有结果 →
  重点与知识点,建议追问从 0 变正 → 概览。自动弹开会打断主对话流(窄屏抽屉
  直接盖住内容),用户点开该段即清提示环;唯一例外是新手引导的 `activateSection`
  指令(收起时目标面板根本不在 DOM 里,必须程序化展开);
- 状态徽标与下载/打印按钮住在 rail 底部:`.detail-sidebar-header` 收起时不存在,
  它们若跟着标题行消失就完全不可达(任务状态在本页仅此一例展示);
  下载/打印的显示条件保持原逻辑(任务 completed 或有结果时才出现),状态徽标始终显示;
- 键盘:ArrowUp/Down 在四个段按钮间移动焦点,Escape 收起(原生 button 的
  Enter/Space 即展开)。

### 5.2 展开面板标题行(detail-sidebar-header)

```
┌───────────────────────────────────┐
│ 审查结果 (12)                     │
└───────────────────────────────────┘
```

- 标题 = **当前段名 + 该段规模**(如「审查结果 (12)」):段名取 `activeSectionLabel`,
  计数取 `activeSectionCount`(审查结果 = `reviewItems.length`,重点与知识点 =
  `task.results.length`,与段内卡片同源;概览是混合区块不给计数,核查过程的时间线
  条数不是用户要的“规模”也不给;0 不显示,空态交给段内 empty hint);
- **字号阶梯**(本栏只认这一套):标题行 `--fs-base`(15px)/`--fw-bold`(700)
  > 段内小标题 `--fs-sm`(14px)/600(对话模型 / 任务清单 / 动态验证 / 建议追问方向 /
  核查过程 panel-title / 分档头 / 分组头) > 卡片正文 14px。
  不用 `--fs-lg`(18px):那是让侧栏标题大过主区任务标题(15px),而本栏的设计
  前提就是“右栏每一 px 都是从主对话流身上扣的”;15 vs 14 只差 1px,拉开靠字重
  与段内降级——概览那几个小标题原先是 15px,比段名自己还大(已降);
  计数 `.count` 保持 14px/400 淡色,从属于标题;
- 标题是 **`<h2>` 而不是 `<span>`**:段名从段内 h2 上移过来后,段内正文仍有 h3(分档
  头)/ h4(卡片标题),需要一个段级标题做它们的父级;这一行也就是 `#detail-panel`
  这个 `role="region"` 内唯一的可见标题(区名由 `aria-label` 另行交代);
- 标题行**不放控件**——收起在 rail(再点当前段),状态徽标与导出在 rail 底部;
  也不放临时结果提示与段内动作(它们留在段内首行,见 §5.3);
- 栏级总名「核查与结果」不再钉在标题行:它对「正在看哪一段」零信息量,而原先跟在后面的
  那个小段名标签才是有信息量的,两者合并成一个(总名仍保留在 rail 的 `aria-label`
  「核查与结果分段」与新手引导措辞里,不作为面板标题)。

### 5.3 四段内容(sidebar-panel,按 activeSection 展示)

四段共用 `#detail-panel` 滚动容器,面板用 `v-show` 切换(保持 Agent2Panel 的
轮组展开状态与流式卡片不因切段而丢,与原先 tab 行为一致)。

#### 段 1:概览

1. **当前阶段 / 错误**(条件渲染:`(isRunning && current_stage) || error_message`):
   仅活跃期(pending/running/paused)显示"当前阶段",失败任务显示错误信息;
   任务进入终态且无错误时整块收起。场景/创建时间/完成时间已移除——
   创建时间在主区标题行,完成时间改为悬浮 rail 状态徽标(title)查看。
   不再包含:状态徽标与下载/打印按钮(已移至 rail,见 §5.1)、
   用户意图卡片(不再显示;用户指令仍保留在对话流顶部 userDirective 气泡)。
2. **对话模型**(`TaskRuntimeSettings` 的 `variant="panel"` 形态,`v-if="task"` 故 pending 也可见):
   AI助手 / 检查助手 两个模型选择框 + 「实际使用」解析行。与输入框上方「运行时设置」
   折叠条是**同一个组件**(bar 形态原样保留),因此选项、生效模型解析、保存链路全都共用,
   两处入口不会显示不同的值;保存成功后都经 `@saved` 回填 task 快照。
   解析链与后端 orchestrator 同一口径(`task.react_llm_config_id` → `task.llm_config_id`
   → 任务默认审查模型槽位 → 环境默认;外部 CLI 执行器的 react 模型由 CLI 自管,
   只报「由 CLI 自管」不编名字),纯函数在 `utils/taskModels`(单测 `taskModels.test.ts`),
   来源数据加载在 `composables/useTaskRuntimeModels`(LLM 清单复用 `useLlmConfigOptions`
   缓存,槽位只合并并发请求不做长期缓存)。
   「实际使用」行只在任务**没有**直接指定模型时出现(留空 / 原选配置已被删除 / 走默认档):
   选择框本身已写着配置名时再复述一遍是噪音;配置被删那种额外带「原选配置已删除,已回退」。
   生效时机同 bar 形态:running/paused 下的修改在下一轮执行才生效,组件内 toast 说明。
3. **任务清单**(有 plan 时):原主对话流"计划清单"卡迁入,展示最新一轮的
   计划步骤(✓ done / ◌ in_progress / ○ pending)与进度 x/y;数据来自
   SSE plan 事件与历史对话提取(`<plan>` 块 / TodoList tool_call),
   随运行实时更新(`latestPlanSteps`)。
4. **动态验证**(配置了测试环境 URL 时):开关、授权模式切换、登录凭证(脱敏);不出现 verifier_agent 字样。
5. **建议追问方向**(两组 `SuggestionCards`,有建议时):原内联在 Agent2Panel 底部,
   现随分段重构上移到概览——核查段只留过程,概览承担"现状 + 下一步"。两组同形
   (`content` 均为 `{"suggestions":[...]}`),仅按 type 分流,type 清单的唯一来源是
   `utils/agent2Suggestions.SUGGESTION_CONV_TYPES`(新增一类不必挨个改消费点):
   - **检查助手发现的缺口** `type=suggestions` —— 经核查确认的真实缺口;
   - **你可能还想深入了解** `type=followup_suggestions` —— 后台推荐器产出的
     **跨维度探索**(`app/services/followup_recommender.py`,不经核查、推测性,
     关闭检查助手时也有这一组)。
   两个提取函数(`extractSuggestions` / `extractExploreSuggestions`)均取**最新一条**
   同类型记录(整块覆盖语义;非法 JSON / 非数组 / 空白项静默回空),每条一个「追问」
   按钮;两类记录都**不进「核查过程」时间轴、也不点亮该段**(`isSuggestionConv`):
   否则一条 JSON 会在单 agent 模式凭空弹出一个核查面板,还把内部数据契约铺给用户。
   点击后把建议原文当用户消息发出走 resume 链路(`handleSuggestionDig`);
   **防重复锁 `diggingSuggestion` 由 `handleMessageSent` 在发送成功时显式解除**——
   不能只靠 `watch(review_status)`:单 agent 模式后端全程不写该字段(null → null
   不触发),点一次就锁到刷新;被拒 / 异常时 `finally` 也立即解锁(旧实现只靠
   review_status 解锁,发送失败后按钮会一直灰着无法重试)。
#### 段 2:核查过程

**检查助手核查**(Agent2Panel,有 agent2 活动时):agent2 的全部过程输出——
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

#### 段 3:审查结果

**审查项清单**(原与其他段合并在一个 tab 里,现为独立段):`ReviewItem` 按
`utils/reviewBucket.bucketizeReviewItems` 三分(已核实成立 / 已核实不成立 /
待核实 —— 分档主语是"检查助手对这条待核实对象的核实结果",不是"你的项目里有几类
条目";旧口径 发现风险 / 已核查·剔除误报 / 缺口·待改进 让用户分不清在说谁),
每档下紧凑卡片(标题 + 置信档位/分数 + 来源/严重度),点击开
`DetailCardDrawer` 看正文与证据;置信度由后端核验证据引用后派生,模型不自报。
**待核实档不显示置信档位**(它本就没证据):`utils/reviewBucket.pendingVerifyBadge`
按成因给标签(未取证 / 部分核实 / 未核实 / 证据未核验),旧文案"未分级 · —"不再铺满。
来源也不直铺英文枚举:`ORIGIN_LABELS` 把 `agent1_claim` 等显示成「AI助手 的结论」
「你的要求」「领域经验」(后端报告导出共用同一份口径 `app/models/audit.ORIGIN_LABELS`)。
**段内不再有「审查结果 (N)」同名 h2**(段名与计数已上移标题行,见 §5.2),
只剩一条 `sidebar-section-status` 状态行,且仅在 running 时渲染(「检查助手核查中」,
审查项边核边落);完成态卡片直接顶到标题行下。
有建议追问时,状态行下方给一条「N 条建议追问」跳转(`sidebar-jump`,文案不带
「· 去概览」——按钮上不重复说目标段,去向放到 `title` 「到「概览」段查看建议追问方向」里;
概览按钮自己带着条数角标,不提醒会明显降低这个 resume 入口的可达性)。

#### 段 4:重点与知识点

**知识点清单**(原"结果清单",与审查结果拆开后独立成段):agent2 done=true 提炼的
   3-8 条精选知识点(老任务为全量发现,兼容);含 `learning_note` 的卡片
   带"值得学"徽标,展开时正文上方显示学习点引用块;按 `task.params._grouping`
   动态分组(如按严重度,老任务);文件类 meta 标签可点击打开左侧工作区文件。
   段内首行(`sidebar-section-status`,**不再重复段名**,计数在标题行)只承担临时结果
   提示与出题入口:提示随 review_status 分流,running →「检查助手整理中」
   (呼吸点,完成后自动更新),stopped →「检查已终止」(静止中性点:临时结果
   就是最终结果,不再等知识点替换;failed 不在这里另开提示,核查过程段 badge「检查失败」已说明)。
   卡片自己的标题(`result-header > h4` 直接渲染 `r.title`,无前端映射)同样只有
   三条出口:被知识点替换 / 「执行结果(检查已终止)」/ 关闭提炼时定稿为
   「执行结果(未提炼知识点)」 —— done 却没改标题就是 bug(侧栏 badge 已「检查完成」
   而卡片还写着「整理中」)。
   入口为练习题(`practice-generate-btn`,待确认候选题计数时文案换成「确认练习题(N)」)
   与出题进度跳转(`gen-progress-entry`,与前者互斥),它们作用于知识点,因此跟知识点段走,
   不在审查结果段。三者全部不成立时(`showKnowledgeToolbar`)整行不渲染。

> 历史说明:覆盖度看板(task.checklist 驱动的维度卡片网格)已随覆盖度清单功能移除。

### 5.4 展开面板左缘调宽手柄(detail-resize-handle)

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

### 5.5 卡片详情 Drawer(DetailCardDrawer):定位基准是 `.page-body`,不是视口

- **布局**:遮罩 `position: absolute; inset: 0` 铺在 `.page-body` 里(它已是
  `position: relative` + `overflow: hidden`),面板作为遮罩的 flex 子项
  (`justify-content: flex-end` + 默认 `align-items: stretch` 撑满高),自身不再 `fixed`;
- **为何不 Teleport 到 body,也不写 `top: <顶栏高>`**:顶栏高度是内容撑出来的
  (本视图 `leading` 插槽里的历史任务按钮 34px,首页没有),任何常量都会飘;交给
  布局 containment,上缘自然落在 AppHeader 下缘,与 rail / 侧栏表头连成一条水平缝。
  同族口径见 `PracticeGenerateSidebar` 覆盖态(注释已写明「定位基准为宿主 .page-body」);
- **得到**:抽屉开着时顶栏照常亮着——主题切换 / 帮助 / 主导航 / 到期复习角标都直接可点,
  不用先关抽屉;`aria-modal="true"` 因此拿掉(它不再是真 modal),`role="dialog"` +
  `aria-label` 保留;
- **不变**:主对话流、左侧历史任务栏、右侧 rail 仍在遮罩之下(与改前一致),Esc / 点遮罩 / 关闭
  按钮三条出路也没动;
- **宿主约束**:组件必须写在 `.page-body` 内部(本视图 `<nav class="detail-rail">` 之后)。
  挪到 `.page` 末尾或改回 Teleport,定位基准就退回视口,抽屉重新盖住顶栏;
- **窄屏**:≤640px 面板 `width: 100%`——不再写 `100vw`(基准已是容器,视口单位在有
  纵向滚动条的桌面浏览器上会比容器宽几个像素,被 `overflow: hidden` 削掉);
- **审查项正文顺序(字段语义改造后)**:待核实对象(`review_target`)→ 审查结论
  (`conclusion`,分析过程)→ **原始证据(`evidence.source`,保留文件:行 + quote)**→
  分析依据 → 验证测试 → **可追问内容(`follow_up`)**。旧标签「被核实对象 / 发现问题 /
  建议」已换名(旧 `description` 里装的是全文分析,标成"发现问题"与正文不符);
  可追问内容带一个按钮,点击把这句作为用户消息发出走 resume(`emit('dig')` →
  视图层 `handleSuggestionDig`),**共用侧栏建议卡的 `diggingSuggestion` 锁**——
  否则抽屉里的追问与侧栏建议可以同时发两条,新一轮执行互相踩。

## 6. 全局弹窗

| 弹窗 | 触发 |
|---|---|
| VerifyActionDialog | 动态验证 per_action 模式,逐动作授权 |
| CommandConfirmDialog | local 模式危险命令确认 |

> 这两个是真 modal:Teleport 到 body + 遮整个视口。`DetailCardDrawer` **不属于本表**——
> 它是阅读面板,基准 `.page-body`、不遮顶栏、不带 `aria-modal`(见 §5.5)。

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
| body 顺序:任务概览 → 动态验证 → 结果清单 | body 顺序:任务概览 → 动态验证 → 结果清单(本次改动只调控件位置;现行完整分段已进一步演进为「概览(任务清单/动态验证/建议追问) / 核查过程 / 审查结果 / 重点与知识点」,以 §5.3 为准) |
| 状态徽标与下载/打印按钮在"任务概览"区块顶部(overview-header) | 迁入标题行;概览区块后又精简为仅活跃期"当前阶段"+错误信息(场景/创建/完成时间移除,见 §5.3。注:标题行这一档已在 §10 再移到常驻 rail) |
| 任务概览含用户意图卡片(user_input 的 Markdown 渲染) | 移除,不再显示(对话流顶部 userDirective 气泡仍保留用户指令) |

保留不变:下载/打印按钮的显示条件(completed 或有结果)、导出逻辑
(exportMarkdown / exportPdf)、结果卡片折叠交互。

> 本节为当时那次改动的对照表。“覆盖度”区块在更早的“覆盖度清单”功能移除时就已经
> 不存在了(见 §5.3 历史说明),旧表两列均不应含它,已更正。

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
| 主对话流每轮顶部显示"计划清单"卡 | 移至右侧栏"任务清单"(最新一轮计划+实时进度,见 §5.3 段 1) |
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

## 10. 右侧栏改为常驻分段 rail(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 顶部横排三 tab(概览 / 核查 / 结果)+ 标题行折叠开关 + 折叠态右上角悬浮把手:三套展开入口 | 常驻竖向 rail 上的四个 disclosure 按钮,**rail 就是折叠态**(`activeSection: key \| null` 单一状态;`detailCollapsed` / `activeSidebarTab` / `userPickedTab` 均删除) |
| 「结果」tab 同时装审查结果与重点与知识点,角标是两者相加 | 拆为「审查结果」与「重点与知识点」两段,各自角标(`reviewItems.length` / `task.results.length`) |
| tab 语义 `role="tablist" / tab / tabpanel` | disclosure 语义:按钮 `aria-expanded` + `aria-controls="detail-panel"`,面板 `role="region"` + `aria-label` 取段名 |
| 审查状态变化**自动抢切 tab**(running→核查,done→结果),闸门 `userPickedTab` | **不自动展开**,只写 `attentionSection` 点亮提示环与角标(自动弹开会打断主对话流;窄屏抽屉直接盖住内容) |
| 建议追问方向内联在 Agent2Panel 底部 | 上移到「概览」段(`SuggestionCards` 组件 + `utils/agent2Suggestions` 纯函数);审查结果段加一条「N 条建议追问 · 去概览」跳转,概览按钮带条数角标 |
| 「核查」段名与拆出来的「审查结果」近义撞车 | 改名「核查过程」,与提示词里“审查过程”措辞对齐 |
| 状态徽标 + 下载/打印在 `.detail-sidebar-header` 内 | 移到 rail 底部(面板收起时标题行不存在,否则任务状态与导出入口都不可达);标题行改为总标题 + 当前段名 + 收起按钮 |
| 折叠/展开与段选择不持久化 | 展开段按用户存 `secondlook:sidebar-section:task-detail:{email}`(空串 = 收起);不复用 `buildStorageKey`,它前缀写死 `sidebar-width` |
| 引导 v5:`activateTab` 机制 + `detail-results` 指 'results' | 引导 v6:`activateSection`(程序化展开,收起时目标不在 DOM)、`detail-results` 改指 'knowledge'、新增 `detail-rail` 与 `detail-findings` 步骤(后者接上此前无人引用的孤儿锚点 `data-onboarding="detail-review"`) |
| 追问防重复锁仅靠 `review_status` 变化释放 | 发送被拒 / 异常时在 `finally` 立即解锁(旧实现下失败后按钮会一直禁用) |

保留不变:栏级总名“核查与结果”(页面仍叫任务详情页)、面板四段的内容本体、
`v-show` 切段(不丢 Agent2Panel 轮组展开状态)、拖宽与双击复位(`useResizableSidebar`
零改动,断点仍 1024px)、导出报告的数据层拼装(`routers/tasks.py` 已按审查项 /
知识点分节,UI 分段不影响它)。

实现要点(供代码改动参考):

1. 段定义 `SIDEBAR_SECTIONS`(key/label);rail 只放图标,`title`/`aria-label` 载段名,
   段名同时在展开标题行 `.detail-sidebar-section` 可见。
2. rail 是 `.detail-sidebar` 的兄弟节点(同在 `.page-body` flex 行内);
   窄屏下两者各自 `position:absolute`,抽屉 `right: var(--detail-rail-width)` 给 rail 让位。
3. 建议文本解析走 `extractSuggestions(task?.conversations)`,取最新一条 `type=suggestions`;
   `diggingSuggestion` 锁属视图层(它要调 `sendTaskMessage`),Agent2Panel 不再
   `emit('send-suggestion')`。
4. 引导事件名 `sidebar-activate-section`(旧 `sidebar-activate-tab` 已废弃);
   未知 key 仍由 `SIDEBAR_SECTIONS.some()` 守卫静默丢弃——改段名时记得同步引导步骤。

## 11. 标题行去控件化:收起按钮回归 rail + 标题改段名(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 标题行 = 总标题「核查与结果」+ 小段名标签 + `WorkspaceToggleButton`(收起为分段栏) | 标题行 = **当前段名**一个纯文本,无控件(现进一步演为段名 + 计数的 `<h2>`,以 §12 为准) |
| 收起有两条鼠标路径(标题行按钮 / 再点当前段按钮),是 §10「三套展开入口收敛为 rail 四个 disclosure」之后漏网的第四个入口 | 收起只剩 rail 当前段按钮一条鼠标路径(`title`/`aria-label` 带「再次点击收起」补足可发现性) |
| 栏级总名占着标题行主位,段名退为附属小标签 | 段名上位成标题(`activeSectionLabel`);栏级总名退到 rail `aria-label` 与引导/帮助文案 |

保留不变:栏级名「核查与结果」作为这一栏的概念称呼(导航 `aria-label`、help.md、
agent2 提示词里的指称不变),左侧历史任务栏在 `AppHeader` 里的 `WorkspaceToggleButton`
(它没有 rail 等价物,不冗余),`collapseDetail()`(仍由 Escape 使用)。

实现要点(供代码改动参考):

1. 删 `WorkspaceToggleButton` 与 `.detail-sidebar-section` 两个节点及其 CSS;
   `WorkspaceToggleButton` 的 import 仍被 `AppHeader` 插槽使用,不要一并删掉。
2. 新增 `railBtnLabel(sec)`:展开态 `${sec.label}（再次点击收起）`,收起态回 `sec.label`,
   同时接 `title` 与 `aria-label`(无障碍名也跟着说清动作)。
3. 已知缺口:Escape 处只在 rail `<nav>` 的 `keydown` 上,焦点在面板正文里时按 Escape
   不会收起。标题行原先那个按钮是此场景唯一的鼠标退路,现已移除——若要补,
   把同一处理器挂到 `<aside class="detail-sidebar">` 上即可。
4. 引导步骤 `detail-rail` 早已写明「再点一下当前段就收起」,本次改动后它成了唯一路径,
   文案无需改。

## 12. 段名不再一处两遍:标题行扛段名与计数(本次改动)

§11 把段名提为标题后,与段内原有的同名 `<h2>` 撞成字面重复(审查结果 / 重点与知识点
两处各读一遍)。**该重复在 §11 之前就已存在**(当时以 `.detail-sidebar-section` 小标签
的形式与段内 h2 并存),§11 只是把它从重音提到了标题级——所以回退 §11 并不是解法。

职责划分:**标题行管身份与规模,段内首行管状态与动作,rail 管导航。**

| 旧结构 | 新结构 |
|---|---|
| 标题行「审查结果」+ 段内 `<h2>审查结果 (N)</h2>` | 标题行 `<h2>审查结果 (N)</h2>`;段内同名 h2 删除,仅 running 时留一条 `sidebar-section-status` 状态行 |
| 标题行「重点与知识点」+ 段内 `<h2>重点与知识点 (N)</h2>` + 提示 + 出题入口 | 计数上提标题行;段内行降为 `div.sidebar-section-status`,只留临时结果提示 + 出题入口,且由 `showKnowledgeToolbar` gate（临时结果提示与出题入口两类都不出现时整行不渲染） |
| 标题是 `<span>`,段内 h2 才是真标题 | 标题行用 `<h2>`:段内仍有 h3/h4,需要段级标题做父级,否则 `#detail-panel` 干脆没标题 |
| 空态文案「暂无重点与知识点」重申一遍段名 | 「任务收尾时由检查助手提炼学习点」 |
| 段名/计数只能随正文滚走 | 标题行不滚（`flex-shrink: 0`），段名与规模在滚动期间常驻可见 |

保留不变:概览段无自名标题(正文开头是任务清单/动态验证/建议追问方向),所以标题行
对它是唯一身份来源——这也是不能反过来删标题行留段内 h2 的原因;核查过程段的
`Agent2Panel` 标题「检查助手核查」**不改**:它点名的是行为主体(带状态 badge 与
「终止检查」按钮),与段名「核查过程」不是字面重复,且 help.md 里「点『检查助手核查』
标题行的『终止检查』」就靠它定位;`v-show` 切段、面板 `role="region"` + `aria-label`、
审查结果段的建议追问跳转与模型提示段落位置。

实现要点(供代码改动参考):

1. 新增 `activeSectionCount`(与段内卡片同一数据源,不在模板里直写 `reviewItems.length`
   以便“哪些段有计数”这件事只有一处口径);定义位置紧跟 `activeSectionLabel`,
   它引用的 `reviewItems` 在其后声明——computed 懒执行,不会踩 TDZ(先例
   `sectionBadge` 同样如此)。
2. 段内行用 `div.sidebar-section-status` 而非标题元素(它已不是标题),因此
   `.sidebar-results h2` 这条 CSS 是死规则,直接替换掉(原「重点与知识点」分区标题注释
   一并换);行内 margin 不能简单归零——外层 `.sidebar-panel` 是 flex(gap 只作用于
   section 之间),section 内部是普通块流,**垂直 margin 会折叠**:`margin-bottom:
   var(--space-2)` 与相邻块的 margin 叠加后取最大值,因此审查结果分档头与知识点分组头
   的间距与改前一致,仅「本次出题将使用」提示紧 4px。
3. `.practice-generate-btn` / `.gen-progress-entry` 都带 `margin-left: auto`,靠的是
   父行 flex:改成 `div` 后仍是 flex,动作按钮依旧靠最右——不需要动它们的样式。
4. 本仓无 `@vue/test-utils`(devDependencies 里没有,现有 20 个 spec 全部是
   纯函数/composable),所以本轮改动靠 `vue-tsc --noEmit` + `vite build` +
   全量单测回归,没有新增组件级断言。若要覆盖 `showKnowledgeToolbar` / `activeSectionCount`,
   得先把它们抽成 `utils/` 里的纯函数(与 `utils/reviewBucket` 同族口径)再写单测。

## 13. 卡片详情 Drawer 改为贴在顶栏下方(本次改动)

| 旧结构 | 新结构 |
|---|---|
| Teleport 到 body,遮罩 `fixed inset:0` + 面板 `fixed top:0` —— 整视口高,顶栏被压暗且被面板盖住右侧(主题/帮助/设置/登出都要先关抽屉) | 不 Teleport,写在宿主 `.page-body` 里;遮罩 `absolute inset:0`,面板作 flex 子项撑满 —— 上缘自然对齐顶栏下缘 |
| 顶栏高度若要用 `top` 避开,只能写常量 | 常量干脆不需要:交给布局 containment(顶栏高度由内容撑出,各页不一) |
| `role="dialog" aria-modal="true"` | 去掉 `aria-modal`(顶栏真的可达了,留着就是给读屏报一个假的 inert) |
| 手机档 `width: 100vw` | `width: 100%`(基准已是容器) |

保留不变:遮罩仍盖住主对话流 / 历史任务栏 / rail,Esc 与点遮罩关闭、背景 overflow 锁、
滑入动画(`translateX(100%)`,与定位方式无关)。`VerifyActionDialog` / `CommandConfirmDialog`
等真 modal 不改。

实现要点(供代码改动参考):

1. 模板根由 `<Teleport>` 换成 `<Transition>`(整块内容少两级缩进),遮罩与面板
   的 `z-index` 不再需要两级:遮罩 1000 带着面板一起排层即可。
2. 宿主放置位置是这个改动的**唯一不能随手改的约束**:`.page-body` 里 `<nav class="detail-rail">` 之后。
   两边都写了注释互指,免得下次重构把它“归位”到页尾。

## 14. 右侧栏标题行字号修正(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 标题行段名 `--fs-sm`(14px)/600,与段内卡片标题同大小——看不出谁是头 | `--fs-base`(15px)/`--fw-bold`(700),全栏最大最重 | 
| 概览段三个小标题(任务清单 / 动态验证 / 建议追问方向)`--fs-base`(15px),**比段名自己还大** | 降到 `--fs-sm`(14px)/600,与核查过程 panel-title、分档头、分组头对齐 |
| 可选方案:标题行直接上 `--fs-lg`(18px) | **未采纳**:侧栏标题会大过主区任务标题(15px),与“主对话流是页面核心”相冲 |

保留不变:主区 `.conv-header-title` 仍为 15px/600(与段名同大小不同字重,
两栏分属不同区域);标题行高度只动 1-2px(`--lh-tight` 下 14→15px),
不换 padding;rail 图标/角标与段内卡片样式不动。改动由用户提出(“标题字号太小”),
本质是标题层级倒挂:段名 14px 小于它自己的下级标题 15px。

## 15. 追问跳转文案缩短 + 知识点段动作靠左(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 跳转按钮文案「N 条建议追问 · 去概览」 | 「N 条建议追问」;去向移到 `title`(悬浮可见),rail 的概览角标已带条数 |
| 「生成练习题」/「正在出题·查看进度」用 `margin-left: auto` 钉到段内首行最右 | 两个都靠左,跟临时结果提示成一组;它们本来就是同一位置的二选一,靠右会在状态切换时左右跳,且孤在右缘看不出它管的是下方卡片 |
| `.sidebar-jump { align-self: flex-start }` | 删除:父层 `.sidebar-review` 是 `display: block`,这行是父层还是 flex 面板时的残留,本来就不起作用 |

保留不变:段内首行仍只在有内容时渲染(`showKnowledgeToolbar`),没有因此把动作推到
面板标题行(§12 的职责划分照旧);`margin-left: auto` 从两个入口都摘掉了,只保
一处口径——不要只改一个,否则出题进行中与完成后按钮会位置不同。

## 16. 概览段增加「对话模型」块(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 概览:当前阶段/错误 → 任务清单 → 动态验证 → 建议追问方向 | 概览:当前阶段/错误 → **对话模型** → 任务清单 → 动态验证 → 建议追问方向 |
| 改模型只有输入框上方「运行时设置」折叠条一个入口(箭头点开才看得到,且 pending 状态整块不渲染) | 概览段内嵌同一组件的 `variant="panel"` 形态:无折叠条、常显、pending 也在;折叠条入口原样保留 |
| 生效模型(留空到底用哪个)只在前端各入口自己拼的选项文案里,说一半 | 解析链与文案收到 `utils/taskModels`(纯函数 + 单测),来源加载收到 `composables/useTaskRuntimeModels`,两处入口共用 |

保留不变:bar 形态的位置/折叠交互/toast/`@saved` 回填;rail 的概览角标仍取建议追问条数
(模型块不参与角标与提示环——它是常驻配置信息,不是要用户回来看的新内容);面板标题行
对概览仍不给计数;PATCH `/tasks/{id}/runtime_config` 端点与后端解析链零改动。

实现要点(供代码改动参考):

1. 「实际使用」行的出现条件是 `modelsReady && source !== 'task'`:任务直接指定了模型时
   选择框已经写着名字,复述只会挤占栏宽;留空 / 原选配置被删 / 走默认槽位这几种才需要
   交代落点。配置被删那一种额外带「原选配置已删除,已回退」——不能把一个已不存在的
   配置报成在用。CLI 执行器的说明走原有 `.rs-hint`(选择框被那句提示替掉),不再叠一行。
2. 两处入口同批挂载时只发一次 `GET /models/defaults`(`ensureTaskModelSources` 只合并
   **在途**请求,不做长期缓存):设置页改过「任务默认审查模型」后再进详情页要立刻看到
   新默认,长期缓存会让它报上一个模型。侧栏本身是 `v-if="task && activeSection"`(收起即
   卸载),因此每次展开概览会重取一次默认槽位——这就是“不读旧默认”换来的代价(一次
   轻请求),不是漏配缓存。LLM 清单那边已有 `useLlmConfigOptions` 缓存
   (登出与清单增删改都会作废),此处只复用不自建。
3. 内嵌态的样式差异全压在 `.is-embedded` 一组选择器上(撑满栏宽、去掉浮层边框与阴影、
   小标题走侧栏 14px/600 档),bar 态的 CSS 一字未改——两态共用模板与脚本,
   不会因为改一处把另一处的手感顺手动掉。

上线前自查出来的两个口径问题(同根因:**数据没到位时不该下结论**),一并收口:

4. 加载期不再报「原选配置已删除」:清单在途时 `configs=[]`,`resolveReviewModel`
   找不到配置就会被判成 stale,而「实际使用」行不受 loading 控制 → 直刷进详情页
   会先闪出「环境默认(原选配置已删除,已回退)」再说真话。现改为
   `modelsReady = !modelSourcesLoading && llmConfigsLoaded`,未就绪时整块只给状态行
   (「模型信息加载中…」/「模型清单未加载成功,刷新页面重试」),两个选择框同时禁用
   ——拿着不完整的清单让人保存更容易选错。只看 `modelSourcesLoading` 不够:它只跟踪
   槽位请求,清单拉挂时它照样翻 false。
5. `utils/taskModels` 的 stale 改要 `configsLoaded`:清单没到手时「找不到 id」是**未知**,
   不是已删(请求失败也永远保持 false,不会误报)。选择框因此分两种空状态:
   未就绪 placeholder 为「模型信息加载中…」,就绪后 id 仍不在清单里才是「原选配置已删除」
   (BaseSelect 的 model-value 不在 options 里时归 placeholder,不能再一律拿「请选择」占)。
   不把已删 id 注进 options 当一项展示:选中它就会 PATCH 一个不存在的 id,后端拒 400。
6. `slotLabel` / `envModel` 是按用户私有的展示信息,登出必须清:`auth.ts` 的 `logout()`
   现在除了 `invalidateLlmConfigs()` 还调 `invalidateTaskModelSources()`(同一世代号写法:
   推进世代 + 释掉在途句柄 + 置 loading),否则同标签页换账号时,详情页会先报上一个
   账号的默认模型名——不止「实际使用」行,选择框的默认选项名(印在触发按钮上)会一直
   带着别人的配置名,直到自己的请求落地。
