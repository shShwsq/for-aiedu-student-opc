/**
 * 任务模型默认(模型槽位)API
 *
 * 对应后端 app/routers/model_configs.py 的 /models/defaults 端点。
 * - GET /models/defaults  各任务的默认模型分配(只读聚合)
 * - PUT /models/defaults  写入可配槽位(当前仅「任务默认审查模型」)
 */
import client from './client'
import type { ModelSlotsResponse, SaveModelSlotsRequest } from '@/types/model_slots'

/** 拉取全部槽位的当前分配(聚合页一次请求拿齐,不做前端多路 fan-out) */
export function getModelSlots(): Promise<ModelSlotsResponse> {
  return client.get('/models/defaults').then((r) => r.data)
}

/** 保存可配槽位(llm_config_id 空串=清空回退环境默认;不传的槽位不动) */
export function saveModelSlots(req: SaveModelSlotsRequest): Promise<ModelSlotsResponse> {
  return client.put('/models/defaults', req).then((r) => r.data)
}
