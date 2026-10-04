/**
 * 练习模块 API(对应后端 app/routers/practice.py)
 *
 * 题目来源为审计任务的真实发现,经 LLM 改编为客观题;
 * 组卷策略:到期复习(SM-2)+ 薄弱点强化 + 难度匹配。
 */
import client from './client'
import type {
  ActivateQuestionsRequest,
  ActivateQuestionsResponse,
  ClearRecordsResponse,
  ConfirmQuestionsRequest,
  ConfirmQuestionsResponse,
  DraftQuestion,
  ExplainKnowledgePointsResponse,
  GenerateJobResponse,
  GenerateJobsResponse,
  GenerateJobStatus,
  GenerateModelInfo,
  GenerateRequest,
  KnowledgeExplanation,
  KnowledgePointCard,
  LearningTopicCreateRequest,
  LearningTopicDef,
  LearningTopicUpdateRequest,
  PracticeStats,
  PracticeSummary,
  QuestionDetail,
  QuestionListItem,
  SessionDetail,
  SessionListItem,
  StartSessionRequest,
  StartSessionResponse,
  SubmitAnswerRequest,
  SubmitAnswerResponse,
  TrendResponse,
} from '@/types/practice'

/**
 * 从审计任务生成候选题(draft,异步)
 *
 * 立即返回 job_id,轮询 getGenerateJob 拿进度与结果。
 */
export function generateQuestions(req: GenerateRequest): Promise<GenerateJobResponse> {
  return client.post('/practice/generate', req).then((r) => r.data)
}

/** 本次出题将使用的模型(任务详情页出题入口附近展示用,与真实出题同一解析) */
export function getTaskGenerateModel(taskId: string): Promise<GenerateModelInfo> {
  return client.get(`/practice/tasks/${taskId}/generate-model`).then((r) => r.data)
}

/** 轮询出题进度与结果 */
export function getGenerateJob(jobId: string): Promise<GenerateJobStatus> {
  return client.get(`/practice/generate/${jobId}`).then((r) => r.data)
}

/**
 * 当前用户的 job 列表(运行中优先,限最近 10 条)
 *
 * 练习页侧栏轮询发现正在运行的出题 job(默认 manual/auto);
 * 知识点讲解 job 用 sources=['explain'] 单独取,不混进出题进度。
 * 实时进度与流式输出另走 SSE,见 api/practiceStream.ts。
 */
export function listGenerateJobs(sources?: string[]): Promise<GenerateJobsResponse> {
  const params = sources && sources.length ? { sources: sources.join(',') } : {}
  return client.get('/practice/generate/jobs', { params }).then((r) => r.data)
}

/** 待确认候选题完整内容(可按来源任务过滤) */
export function listDrafts(taskId?: string): Promise<DraftQuestion[]> {
  return client
    .get('/practice/drafts', { params: taskId ? { task_id: taskId } : {} })
    .then((r) => r.data)
}

/** 确认勾选的候选题入库,丢弃其余 draft */
export function confirmQuestions(
  req: ConfirmQuestionsRequest,
): Promise<ConfirmQuestionsResponse> {
  return client.post('/practice/questions/confirm', req).then((r) => r.data)
}

/** 只转正指定 draft(不影响其余 draft) */
export function activateQuestions(
  req: ActivateQuestionsRequest,
): Promise<ActivateQuestionsResponse> {
  return client.post('/practice/questions/activate', req).then((r) => r.data)
}

/** 按需即时组卷(答案不下发) */
export function startSession(req: StartSessionRequest): Promise<StartSessionResponse> {
  return client.post('/practice/sessions', req).then((r) => r.data)
}

/** 提交单题答案,返回判分结果与知识点记忆状态更新 */
export function submitAnswer(
  sessionId: string,
  req: SubmitAnswerRequest,
): Promise<SubmitAnswerResponse> {
  return client.post(`/practice/sessions/${sessionId}/answers`, req).then((r) => r.data)
}

/** 练习统计:能力值 / 到期复习数 / 薄弱点分布 */
export function getPracticeStats(): Promise<PracticeStats> {
  return client.get('/practice/stats').then((r) => r.data)
}

/** 知识点卡片列表(知识点看板视图):全量知识点 + SM-2 状态 + 题数 + 分栏状态 + 讲解 */
export function listKnowledgePoints(): Promise<KnowledgePointCard[]> {
  return client.get('/practice/knowledge-points').then((r) => r.data)
}

/**
 * 按需生成/更新知识点讲解(异步)
 *
 * 立即返回 job_id,轮询 getGenerateJob 拿进度(讲解不接 SSE,1~3 次调用就够)。
 * force=true 重写已有 auto 讲解;manual(用户自己写的)任何时候都不会被覆盖。
 */
export function explainKnowledgePoints(
  knowledgeKeys: string[],
  force = false,
): Promise<ExplainKnowledgePointsResponse> {
  return client
    .post('/practice/knowledge-points/explain', {
      knowledge_keys: knowledgeKeys,
      force,
    })
    .then((r) => r.data)
}

/** 手工编辑知识点讲解(写后 source=manual,自动生成不再覆盖) */
export function saveKnowledgeExplanation(
  knowledgeKey: string,
  markdown: string,
): Promise<KnowledgeExplanation> {
  return client
    .put(`/practice/knowledge-points/${encodeURIComponent(knowledgeKey)}/explanation`, {
      markdown,
    })
    .then((r) => r.data)
}

// ---- 学习主题(用户可管理词表:内置 4 个 + 自定义) ----

/** 学习主题列表(懒播种内置主题),按 sort_order 排序,附每主题知识点数 */
export function listLearningTopics(): Promise<LearningTopicDef[]> {
  return client.get('/practice/topics').then((r) => r.data)
}

/** 新增自定义主题(key 服务端生成;重名/超限 400) */
export function createLearningTopic(
  req: LearningTopicCreateRequest,
): Promise<LearningTopicDef> {
  return client.post('/practice/topics', req).then((r) => r.data)
}

/** 修改主题(内置仅 enabled;自定义可改 name/description/enabled) */
export function updateLearningTopic(
  id: string,
  req: LearningTopicUpdateRequest,
): Promise<LearningTopicDef> {
  return client.patch(`/practice/topics/${id}`, req).then((r) => r.data)
}

/** 删除自定义主题(内置不可删;有关联知识点 400) */
export function deleteLearningTopic(id: string): Promise<void> {
  return client.delete(`/practice/topics/${id}`).then(() => undefined)
}

/** 题库列表(可按状态 / 知识点筛选;mistake=true 只返回答错过的 active 题) */
export function listQuestions(params?: {
  status?: string
  knowledge_point?: string
  mistake?: boolean
}): Promise<QuestionListItem[]> {
  return client.get('/practice/questions', { params }).then((r) => r.data)
}

/** 题目完整信息(含正确答案与解析;列表项只有一行摘要,点开单行时按需拉取) */
export function getQuestionDetail(questionId: string): Promise<QuestionDetail> {
  return client.get(`/practice/questions/${questionId}`).then((r) => r.data)
}

/** 轻量汇总(导航徽章用):到期复习数 + 待确认 draft 数 */
export function getPracticeSummary(): Promise<PracticeSummary> {
  return client.get('/practice/summary').then((r) => r.data)
}

/** 按周聚合的学习趋势(默认最近 8 周) */
export function getPracticeTrend(weeks = 8): Promise<TrendResponse> {
  return client.get('/practice/trend', { params: { weeks } }).then((r) => r.data)
}

/** 历史练习会话列表(新到旧) */
export function listPracticeSessions(limit = 20): Promise<SessionListItem[]> {
  return client.get('/practice/sessions', { params: { limit } }).then((r) => r.data)
}

/** 会话逐题作答明细 */
export function getSessionDetail(sessionId: string): Promise<SessionDetail> {
  return client.get(`/practice/sessions/${sessionId}`).then((r) => r.data)
}

/** 归档题目(不再参与组卷) */
export function archiveQuestion(questionId: string): Promise<{ archived: boolean }> {
  return client.post(`/practice/questions/${questionId}/archive`).then((r) => r.data)
}

/**
 * 清空练习记录(不可恢复)
 *
 * includeQuestions=false:进度归零(流水/会话/记忆状态),题库保留但难度重置;
 * includeQuestions=true:连题目与知识点词典一并删除。
 */
export function clearPracticeRecords(includeQuestions = false): Promise<ClearRecordsResponse> {
  return client
    .delete('/practice/records', { params: { include_questions: includeQuestions } })
    .then((r) => r.data)
}
