/**
 * 任务工作区产物 API
 *
 * 对应后端 app/routers/tasks.py:
 * - GET /tasks/{id}/artifacts          列出产物
 */
import client from './client'
import type { TaskArtifactsResponse } from '@/types/taskArtifact'

/** 列出任务的工作区产物(diff/patch 等),按 created_at 升序 */
export function listArtifacts(taskId: string): Promise<TaskArtifactsResponse> {
  return client.get(`/tasks/${taskId}/artifacts`).then((r) => r.data)
}
