/**
 * 任务 API 模块
 *
 * 对应后端 app/routers/tasks.py 的端点。
 * 后端 POST /tasks 异步执行(立即返回 task_id),进度通过 SSE 端点观看。
 */
import client from './client'
import type {
  Scenario,
  SendMessageRequest,
  SendMessageResponse,
  MessageWithdrawResponse,
  TaskCreateRequest,
  CommandConfirmEventData,
  CommandConfirmRequest,
  CommandConfirmResponse,
  TaskCreateResponse,
  TaskDetail,
  TaskListItem,
  VerifyActionEventData,
  VerifyActionRequest,
  VerifyActionResponse,
  VerifyConfigUpdateRequest,
  RuntimeConfigUpdateRequest,
} from '@/types/task'

/** 列出可用场景 */
export function getScenarios(): Promise<Scenario[]> {
  return client.get('/scenarios').then((r) => r.data)
}

/**
 * 列出当前用户可见的任务(自己的 + 匿名的)
 *
 * 用于侧栏历史任务列表。按创建时间倒序。
 *
 * 可选 q:全文搜索关键词(后端 ILIKE 匹配 title / user_input /
 * conversation.content / conversation.reasoning / result.title / result.content)。
 */
export function listTasks(params?: {
  limit?: number
  offset?: number
  q?: string
}): Promise<TaskListItem[]> {
  return client.get('/tasks', { params }).then((r) => r.data)
}

/**
 * 提交任务
 *
 * 后端立即返回 task_id(后台线程异步执行)。
 * 前端跳转详情页后通过 SSE 接收实时进度。
 */
export function createTask(req: TaskCreateRequest): Promise<TaskCreateResponse> {
  return client.post('/tasks', req).then((r) => r.data)
}

/** 查询任务详情(含对话记录与结果) */
export function getTask(taskId: string): Promise<TaskDetail> {
  return client.get(`/tasks/${taskId}`).then((r) => r.data)
}

/**
 * 下载任务报告(Markdown 格式,触发浏览器下载)
 *
 * 后端返回 text/markdown 附件。
 */
export function downloadTaskReportMarkdown(taskId: string): Promise<Blob> {
  return client
    .get(`/tasks/${taskId}/export`, {
      params: { format: 'markdown' },
      responseType: 'blob',
    })
    .then((r) => r.data)
}

/**
 * 获取任务报告 HTML(打印友好,前端用于新窗口打印为 PDF)
 *
 * 后端返回完整 HTML 文档(含内联样式)。
 */
export function getTaskReportHtml(taskId: string): Promise<string> {
  return client
    .get(`/tasks/${taskId}/export`, {
      params: { format: 'html' },
      responseType: 'text',
      transformResponse: [(x) => x],
    })
    .then((r) => r.data)
}

// ============================================================
// 任务暂停/恢复
// ============================================================

/**
 * 暂停正在运行的任务
 *
 * 后台线程会在下一个检查点(迭代边界/工具调用前)阻塞。
 * task.status 变为 paused,前端把"暂停"按钮切换为"恢复"按钮。
 */
export function pauseTask(taskId: string): Promise<{ status: string; message: string }> {
  return client.post(`/tasks/${taskId}/pause`).then((r) => r.data)
}

/**
 * 恢复已暂停的任务
 *
 * 唤醒在检查点阻塞的后台线程,task.status 变回 running。
 */
export function resumeTask(taskId: string): Promise<{ status: string; message: string }> {
  return client.post(`/tasks/${taskId}/resume`).then((r) => r.data)
}

/**
 * 终止 agent2 后台检查(仅 review_status=running 时可用)
 *
 * 有些对话不需要检查:置标志后立即返回,审查线程在下一个检查点
 * (LLM 流 chunk 边界 / 工具循环边界)协作式收尾,写 review_status=stopped
 * 并推 review_done 事件(前端据此收角标)。
 * 本轮知识点不会写入,保留 agent1 的执行结果。
 * 不能用 pauseTask 走这条路:暂停只接受 RUNNING,而后台审查发生在任务已完成后。
 */
export function stopTaskReview(
  taskId: string,
): Promise<{ review_status: string; message: string }> {
  return client.post(`/tasks/${taskId}/review/stop`).then((r) => r.data)
}

/**
 * 请求跳过预克隆
 *
 * 克隆轮询循环在下一个检查点终止当前 clone,orchestrator 降级为
 * agent1 自主克隆。幂等:重复请求无副作用;克隆已完成时
 * 后端静默忽略(标志任务结束时兜底清理)。
 */
export function skipPreClone(taskId: string): Promise<{ message: string }> {
  return client.post(`/tasks/${taskId}/skip_pre_clone`).then((r) => r.data)
}

// ============================================================
// 任务标题修改 / 任务删除
// ============================================================

/**
 * 修改任务标题
 *
 * 传空字符串等价于清除自定义标题(后端存 null,前端回退到 user_input 截断展示)。
 * 返回最新的任务详情,父组件可据此刷新本地状态。
 */
export function updateTaskTitle(taskId: string, title: string): Promise<TaskDetail> {
  return client.patch(`/tasks/${taskId}/title`, { title }).then((r) => r.data)
}

/**
 * 删除任务
 *
 * 后端级联删除对话/结果,并清理沙箱 session。
 * 删除当前正在浏览的任务后,前端需自行跳转离开详情页。
 */
export function deleteTask(taskId: string): Promise<void> {
  return client.delete(`/tasks/${taskId}`).then(() => undefined)
}

// ============================================================
// 用户补充消息(对话界面下方输入框)
// ============================================================

/**
 * 发送用户补充消息
 *
 * 按 task.status 分发:
 * - running / paused:消息入队(agent1 消费前以"待处理"条目展示在输入框上方),
 *   消费时经 conversation 事件转入对话流
 * - completed:启动新的协作 round(resume_audit_with_message)
 * - pending / failed:返回 accepted=false
 */
export function sendTaskMessage(
  taskId: string,
  req: SendMessageRequest,
): Promise<SendMessageResponse> {
  return client.post(`/tasks/${taskId}/messages`, req).then((r) => r.data)
}

/**
 * 撤回待处理消息(运行中发送、尚未被 agent1 消费的)
 *
 * 成功后后端删除 Conversation 记录并推 user_message_withdrawn 事件
 * (前端移除待处理条目);已被消费则 success=false。
 */
export function withdrawTaskMessage(
  taskId: string,
  messageId: string,
): Promise<MessageWithdrawResponse> {
  return client
    .delete(`/tasks/${taskId}/messages/${messageId}`)
    .then((r) => r.data)
}

// ============================================================
// 失败任务重试
// ============================================================

/**
 * 重试失败的任务
 *
 * 后端按失败阶段自动分流:
 * - 早期失败(无可续进度):从头重跑
 * - 执行中途失败:断点续跑(保留已有进度,round_idx 自动续接)
 *
 * 仅 failed 状态接受,其他状态返回 accepted=false。
 * 重试启动后前端需乐观置 running 并重连 SSE(同 completed 发消息后的处理)。
 */
export function retryTask(taskId: string): Promise<SendMessageResponse> {
  return client.post(`/tasks/${taskId}/retry`).then((r) => r.data)
}

// ============================================================
// 动态验证动作授权(verifier_agent per_action 模式)
// ============================================================

/**
 * 查询任务当前待授权的验证动作
 *
 * 用于刷新页面后恢复授权弹窗。无待授权动作返回 null。
 * 后端 verifier_agent 在 per_action 模式下会推送 verify_action SSE 事件,
 * 若 SSE 事件在连接前已错过,通过此接口拉取当前待授权动作。
 */
export function getPendingVerifyAction(taskId: string): Promise<VerifyActionEventData | null> {
  return client.get(`/tasks/${taskId}/pending_verify_action`).then((r) => r.data)
}

/**
 * 提交用户对验证动作的授权决议
 *
 * 唤醒阻塞等待的 verifier_agent 后台线程:
 * - approved=true:继续执行该 HTTP/PoC 动作
 * - approved=false:跳过该动作,verifier_agent 收到"用户拒绝"反馈
 *
 * 返回 accepted=false 表示当前无待授权动作(可能已答复或任务已结束)。
 */
export function submitVerifyAction(
  taskId: string,
  req: VerifyActionRequest,
): Promise<VerifyActionResponse> {
  return client.post(`/tasks/${taskId}/verify_action`, req).then((r) => r.data)
}

/**
 * 查询任务当前待确认的危险命令(刷新页面后恢复弹窗用)
 *
 * local 模式下,LLM 调用 run_command 执行的危险命令会推送 command_confirm SSE 事件,
 * 若 SSE 事件在连接前已错过,通过此接口拉取当前待确认命令。
 */
export function getPendingCommandConfirm(taskId: string): Promise<CommandConfirmEventData | null> {
  return client.get(`/tasks/${taskId}/pending_command_confirm`).then((r) => r.data)
}

/**
 * 提交用户对危险命令的确认决议
 *
 * 唤醒阻塞等待的后台线程:
 * - approved=true:继续执行该命令
 * - approved=false:跳过该命令,LLM 收到"用户拒绝"反馈
 *
 * 返回 accepted=false 表示当前无待确认命令(可能已答复或任务已结束)。
 */
export function submitCommandConfirm(
  taskId: string,
  req: CommandConfirmRequest,
): Promise<CommandConfirmResponse> {
  return client.post(`/tasks/${taskId}/command_confirm`, req).then((r) => r.data)
}

/**
 * 更新任务的验证器配置(运行时可调)
 *
 * 允许在任务运行界面调整验证授权模式与开关。verifier_agent 每次调用时读取最新配置。
 */
export function updateTaskVerifierConfig(
  taskId: string,
  req: VerifyConfigUpdateRequest,
): Promise<TaskDetail> {
  return client.patch(`/tasks/${taskId}/verifier_config`, req).then((r) => r.data)
}

/**
 * 更新任务运行时配置(agent1 / agent2 评估模型)
 *
 * running/paused 时修改在下一轮执行(completed 后追加消息 / failed 重试)生效。
 */
export function updateTaskRuntimeConfig(
  taskId: string,
  req: RuntimeConfigUpdateRequest,
): Promise<TaskDetail> {
  return client.patch(`/tasks/${taskId}/runtime_config`, req).then((r) => r.data)
}
