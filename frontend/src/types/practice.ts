/**
 * 练习模块类型定义(对应后端 schemas/practice.py)
 */

// ---- 题目生成 / 确认 ----

export interface GenerateRequest {
  task_id: string
  max_findings?: number
  /**
   * 重出开关:默认 false,本用户已就该 finding 出过题的后端会整条跳过(不再付 LLM 成本);
   * 任务详情页「重新出题」显式传 true 才允许对同一发现重出
   */
  force_regenerate?: boolean
}

/** 生成的候选题(draft,预览阶段含答案与解析供校对) */
export interface DraftQuestion {
  id: string
  qtype: 'single_choice' | 'true_false'
  stem: string
  code_snippet: string | null
  options: string[]
  answer_idx: number
  explanation: string
  difficulty: number
  knowledge_key: string | null
  knowledge_name: string | null
  /** 出题形式:repo=基于真实源码,synthetic=改编题(虚构代码) */
  origin: 'repo' | 'synthetic'
  /** 知识点编程语言标签(如 ["python", "sql"]) */
  languages: string[]
  /** 题目引用的源码定位(工作区可用时出题产生;老题为 null) */
  source_file: string | null
  source_lines: string | null
}

export interface GenerateResponse {
  questions: DraftQuestion[]
  skipped_findings: number
}

/** 异步出题句柄(POST /practice/generate 立即返回) */
export interface GenerateJobResponse {
  job_id: string
}

/** 任务出题模型解析结果(GET /practice/tasks/{task_id}/generate-model)
 *
 * 与真实出题同一解析优先级(任务配置 > 练习默认 > 环境默认,
 * 「始终用默认出题模型」开启时跳过任务级),供任务详情页出题入口展示。
 */
export interface GenerateModelInfo {
  /** 本次出题将使用的模型名 */
  model: string
  /** 来源:task=任务自带配置 / default=练习默认出题模型 / env=环境默认 */
  source: 'task' | 'default' | 'env'
}

/** 出题进度与结果(GET /practice/generate/{job_id}) */
export interface GenerateJobStatus {
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  done: number
  total: number
  error: string
  questions: DraftQuestion[]
  skipped_findings: number
}

/** 出题 job 摘要(GET /practice/generate/jobs,与 SSE snapshot 同构) */
export interface GenerateJobSummary {
  job_id: string
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled'
  done: number
  total: number
  error: string
  /** 出题来源:manual(任务详情页手动) / auto(任务完成自动生成) / explain(知识点讲解) */
  source: 'manual' | 'auto' | 'explain'
  task_id: string | null
  task_title: string
  current_finding: string
  /** 当前 finding 已累计的 LLM 输出尾部文本(中途接入兜底) */
  recent_text: string
  /** 出题前工作区恢复的最新状态(中途接入兜底;未触发恢复为 null) */
  restore: GenerateRestoreData | null
  skipped_findings: number
  created_count: number
  started_at: string | null
  /**
   * 已请求停止但尚未进终态(后台线程还没跑到检查点)
   *
   * 侧栏据此显示"正在停止…":协作式取消可能滞后数十秒,
   * 没有这个字段用户会以为按钮没生效(刷新页面也不丢这个中间态)
   */
  stop_requested?: boolean
  /** 本次出题的 finding 上限(「继续出题」重发时沿用) */
  max_findings?: number
  /** 本次出题的重出开关(「继续出题」默认 false:已出过题的发现整条跳过) */
  force_regenerate?: boolean
}

export interface GenerateJobsResponse {
  jobs: GenerateJobSummary[]
}

// ---- 出题进度 SSE 事件(GET /practice/generate/{job_id}/stream) ----

/** 初始快照(连接建立时推送,含 recent_text 供中途接入兜底) */
export type GenerateSnapshotData = GenerateJobSummary

/** 开始处理某条发现 */
export interface GenerateFindingData {
  index: number
  total: number
  title: string
}

/** LLM 输出增量(打字机效果) */
export interface GenerateTokenData {
  delta: string
}

/** 出题前工作区恢复状态(沙箱已清理时重新 clone) */
export interface GenerateRestoreData {
  /** start=开始恢复 / progress=克隆中 / done=恢复成功 / failed=恢复失败降级 */
  phase: 'start' | 'progress' | 'done' | 'failed'
  /** 克隆进度百分比 0-100(progress 阶段才有) */
  percent?: number
  /** git 进度行文本或失败原因(截断 200 字符) */
  message?: string
}

/** 出题工具循环的工具调用记录 */
export interface GenerateToolData {
  name: string
  summary: string
}

/**
 * 收尾知识点讲解阶段(出题完成后顺带批量更新讲解)
 *
 * start=已开始 / done=已完成(written=实际写入数)。
 * 侧栏用它补一行「知识点讲解已更新 N 条」。
 */
export interface GenerateExplainData {
  phase: 'start' | 'done' | 'failed'
  /** 待生成讲解的知识点数 */
  total?: number
  /** 分成几批调用(≤8 个知识点一批) */
  batches?: number
  /** 实际写入的知识点数(done 阶段) */
  written?: number
  /** 所用模型名 */
  model?: string
  /** 失败原因(failed 阶段) */
  message?: string
}

/** 进度计数更新(每处理完一条 finding) */
export interface GenerateProgressData {
  done: number
  total: number
}

/** 终止事件:完成 */
export interface GenerateDoneData {
  created: number
  skipped: number
}

/** 终止事件:失败 */
export interface GenerateErrorData {
  message: string
}

/**
 * 终止事件:用户停止出题
 *
 * 载荷与 done 同构(已生成题数/跳过数)+ done/total 断点进度:
 * 侧栏用它说清"停下之前已生成几题、跑到第几条",并提供「继续出题」。
 */
export interface GenerateCancelledData extends GenerateDoneData {
  done?: number
  total?: number
}

export interface ConfirmQuestionsRequest {
  task_id: string
  question_ids: string[]
}

export interface ConfirmQuestionsResponse {
  confirmed: number
  discarded: number
}

/** 只转正指定 draft,不影响其余 draft(题库管理逐条操作用) */
export interface ActivateQuestionsRequest {
  question_ids: string[]
}

export interface ActivateQuestionsResponse {
  activated: number
}

// ---- 练习会话 ----

export interface StartSessionRequest {
  count?: number
  topic_filter?: string | null
  /** 限定学习主题(learning_topics.key,内置或自定义);与 topic_filter 互斥 */
  learning_topic?: string | null
  /** 题目白名单(错题重练):非空时只从这些 active 题中组卷 */
  question_ids?: string[]
}

/** 组卷下发的题面(不含答案) */
export interface SessionQuestion {
  id: string
  qtype: 'single_choice' | 'true_false'
  stem: string
  code_snippet: string | null
  options: string[]
  difficulty: number
  knowledge_name: string | null
  /** 知识点编程语言标签 */
  languages: string[]
  /** 出题形式:repo=真实代码题,synthetic=改编题 */
  origin: 'repo' | 'synthetic'
  /** 题目来源任务(右侧代码栏据此打开对应工作区;老题为 null) */
  source_task_id: string | null
  /** 题目引用的源码文件(仓库内相对路径;无则不自动定位) */
  source_file: string | null
  /** 题目引用的行区间(如 "120-150" 或 "42") */
  source_lines: string | null
}

export interface StartSessionResponse {
  session_id: string
  questions: SessionQuestion[]
  message: string
}

export interface SubmitAnswerRequest {
  question_id: string
  chosen_idx: number
}

/** 知识点记忆状态(SM-2) */
export interface KnowledgeState {
  knowledge_key: string
  knowledge_name: string
  /** 知识点编程语言标签 */
  languages: string[]
  ease_factor: number
  interval_days: number
  repetitions: number
  due_at: string | null
  attempts: number
  correct_count: number
  accuracy: number | null
}

export interface SubmitAnswerResponse {
  is_correct: boolean
  correct_idx: number
  explanation: string
  state: KnowledgeState | null
  answered_count: number
  total_count: number
  /**
   * 答错时后端下发的该知识点完整讲解(Markdown);
   * 答对或尚未生成时为空串(不必每题都拖一段正文)
   */
  knowledge_explanation: string
}

// ---- 统计 / 题库 ----

export interface WeakPointItem {
  knowledge_key: string
  knowledge_name: string
  /** 知识点编程语言标签 */
  languages: string[]
  attempts: number
  correct_count: number
  /** 错误率 0-1 */
  accuracy: number
  ease_factor: number
  due_at: string | null
}

/** 知识点看板分栏(后端按优先级派生):weak=薄弱 / due=待复习 / mastered=已巩固 / learning=学习中 / fresh=未开始 */
export type BoardStatus = 'weak' | 'due' | 'mastered' | 'learning' | 'fresh'

/**
 * 学习主题 key(learning_topics.key):
 * 内置 security/architecture/coding/contract 或自定义 custom_*
 * 为动态值(用户可管理),统一用 string 表示
 */
export type LearningTopic = string

/** 学习主题定义(用户可管理词表,GET /practice/topics) */
export interface LearningTopicDef {
  id: string
  key: LearningTopic
  name: string
  /** 主题视角说明(出题视角 + 分类依据) */
  description: string
  is_builtin: boolean
  /** 出题开关:false 时不再出新题(存量不动) */
  enabled: boolean
  sort_order: number
  /** 该主题下的知识点数 */
  kp_count: number
}

/** 新增自定义主题请求 */
export interface LearningTopicCreateRequest {
  name: string
  description?: string
}

/** 修改主题请求(内置仅 enabled;自定义可改 name/description/enabled) */
export interface LearningTopicUpdateRequest {
  name?: string
  description?: string
  enabled?: boolean
}

/** 知识点卡片(知识点看板视图,GET /practice/knowledge-points) */
export interface KnowledgePointCard {
  knowledge_key: string
  knowledge_name: string
  /** 粗分类(如 cwe / general) */
  category: string | null
  languages: string[]
  /** 所属学习主题 key(看板按主题分组展示) */
  learning_topic: LearningTopic
  /** 作答统计(无作答记录为 0) */
  attempts: number
  correct_count: number
  accuracy: number | null
  /** SM-2 记忆参数(无作答记录为默认值) */
  repetitions: number
  interval_days: number
  ease_factor: number
  due_at: string | null
  /** 题库中该知识点的 active 题数 */
  question_count: number
  board_status: BoardStatus
  /** 知识点讲解正文(Markdown);空串=尚未生成 */
  explanation: string
  /** 讲解来源:auto=模型生成 / manual=手工编辑(不会被自动覆盖)/ 空=未生成 */
  explanation_source: string
  /** 生成所用模型名(手工编辑为空) */
  explanation_model: string
  /** 最近一次更新讲解的时间(展示「更新于」) */
  explanation_updated_at: string | null
  /** 是否有可供模型依据的题(无题时不亮「生成讲解」) */
  can_generate_explanation: boolean
}

/** 按需生成知识点讲解请求(POST /practice/knowledge-points/explain) */
export interface ExplainKnowledgePointsRequest {
  knowledge_keys: string[]
  /** true 时重写已有 auto 讲解;manual 讲解任何时候都不被自动覆盖 */
  force?: boolean
}

/** 讲解 job 句柄(异步,轮询 getGenerateJob 拿进度) */
export interface ExplainKnowledgePointsResponse {
  job_id: string
  /** 本次计划处理的知识点数 */
  total: number
}

/** 知识点讲解字段(手工编辑后回写) */
export interface KnowledgeExplanation {
  knowledge_key: string
  explanation: string
  explanation_source: string
  explanation_model: string
  explanation_updated_at: string | null
}

/** 知识点主题回写字段(手工改主题后回写) */
export interface KnowledgeTopicOut {
  knowledge_key: string
  learning_topic: string
}

export interface PracticeStats {
  ability: number
  due_count: number
  total_attempts: number
  total_correct: number
  accuracy: number | null
  weak_points: WeakPointItem[]
  active_question_count: number
  draft_question_count: number
}

export interface QuestionListItem {
  id: string
  qtype: string
  stem: string
  difficulty: number
  status: 'draft' | 'active' | 'archived'
  knowledge_name: string | null
  /** 知识点编程语言标签 */
  languages: string[]
  /** 出题形式:repo=真实代码题,synthetic=改编题(老题为 null) */
  origin: 'repo' | 'synthetic' | null
  attempts: number
  accuracy: number | null
  created_at: string
}

/**
 * 题目完整信息(GET /practice/questions/{id})
 *
 * 内容字段与候选题预览同构(含 answer_idx 与 explanation),另附状态 /
 * 归类 / 溯源与本题作答统计,供题库管理与错题复盘使用。
 * 仅在首页复盘场景下发,答题中的组卷题面(SessionQuestion)不含答案字段。
 */
export interface QuestionDetail {
  id: string
  qtype: 'single_choice' | 'true_false'
  stem: string
  code_snippet: string | null
  options: string[]
  answer_idx: number
  explanation: string
  difficulty: number
  knowledge_key: string | null
  knowledge_name: string | null
  origin: 'repo' | 'synthetic'
  languages: string[]
  source_file: string | null
  source_lines: string | null
  status: 'draft' | 'active' | 'archived'
  /** 知识点粗分类(如 injection / auth) */
  category: string | null
  /** 出题时归属的学习主题 key(老题为 null) */
  learning_topic: string | null
  /** 来源任务(跳任务详情页查源码用;老题为 null) */
  source_task_id: string | null
  /** 所属知识点的讲解正文(Markdown);空串=尚未生成 */
  knowledge_explanation: string
  /** 讲解来源:auto=模型生成 / manual=手工编辑 / 空=未生成 */
  knowledge_explanation_source: string
  attempts: number
  correct_count: number
  accuracy: number | null
  created_at: string
}

// ---- 导航徽章 / 历史会话 / 趋势 ----

/** 轻量汇总(导航徽章用) */
export interface PracticeSummary {
  due_count: number
  draft_count: number
}

export interface SessionListItem {
  id: string
  started_at: string
  finished_at: string | null
  question_count: number
  answered_count: number
  correct_count: number
  accuracy: number | null
}

export interface SessionAttemptItem {
  question_id: string
  stem: string
  qtype: string
  knowledge_name: string | null
  chosen_idx: number
  correct_idx: number
  is_correct: boolean
  answered_at: string
}

export interface SessionDetail {
  id: string
  started_at: string
  finished_at: string | null
  question_count: number
  attempts: SessionAttemptItem[]
}

/** 按周聚合的学习趋势点 */
export interface TrendPoint {
  week_start: string
  attempts: number
  correct: number
}

export interface TrendResponse {
  weeks: TrendPoint[]
}

/** 清空练习记录的删除计数(DELETE /practice/records) */
export interface ClearRecordsResponse {
  deleted_sessions: number
  deleted_attempts: number
  deleted_questions: number
}
