/**
 * 「我保存过的 LLM 配置」共享缓存(模块级)
 *
 * 下拉选项同时被多处需要:练习设置(出题/讲解模型)、记忆设置(归纳模型)、
 * 智能体策略(任务默认审查模型)、模型设置的「任务模型默认」页。
 * 与 useAgentTypes 同款写法:并发去重、失败清缓存可重试;各面板不再各发一次
 * GET /models/configs,也不再各自复制一份「名称(厂商 / 模型)」拼接逻辑。
 *
 * 模型清单被编辑(增删改)后请调 invalidateLlmConfigs(),否则别的页面会看到旧列表。
 * 登出也必须调(已在 auth store 接好):列表是按用户私有的,残留会让下一个
 * 登录者在同一标签页里看到上一个账号的模型名。
 *
 * 作废只清缓存不够:在途请求回来会把旧账号的列表重新写进模块缓存并标成
 * 「已加载」,所以每次作废推进世代号并释掉在途句柄,过时的响应直接丢弃。
 */
import { ref } from 'vue'

import { getMyModels } from '@/api/model_configs'
import type { LLMConfigItemOut } from '@/types/model_configs'

/** 已加载的配置列表(未加载完时为空数组) */
export const llmConfigs = ref<LLMConfigItemOut[]>([])

/** 是否已完成过一次成功加载(区分「还没拉」与「拉了但没有配置」) */
export const llmConfigsLoaded = ref(false)

let inFlight: Promise<LLMConfigItemOut[]> | null = null

/** 缓存世代号:每次作废自增。在途请求带着旧世代号回来时丢弃它,否则
 * 「登出→清空缓存→上一个账号的响应才到」会把列表写回来并置 loaded=true。 */
let generation = 0

/** 下拉展示名:`名称(厂商 / 模型)`,无名时回退模型名 */
export function configOptionLabel(cfg: LLMConfigItemOut): string {
  return `${cfg.name || cfg.model || '未命名配置'}(${cfg.provider} / ${cfg.model})`
}

/** 拉取并缓存(已加载则直接返回;force=true 用于模型清单保存后刷新) */
export function ensureLlmConfigsLoaded(force = false): Promise<LLMConfigItemOut[]> {
  if (llmConfigsLoaded.value && !force) return Promise.resolve(llmConfigs.value)
  if (!inFlight) {
    const gen = generation
    const request: Promise<LLMConfigItemOut[]> = getMyModels()
      .then((res) => {
        // 请求期间被作废过(登出 / 清单增删改):这份结果已属于上一个账号或
        // 上一份数据,不写回缓存
        if (gen !== generation) return llmConfigs.value
        llmConfigs.value = res.llm_configs
        llmConfigsLoaded.value = true
        return llmConfigs.value
      })
      .catch((err) => {
        // 静默失败由调用方决定(下拉只剩「跟随系统默认」),这里只清缓存允许重试;
        // 过时请求的失败不能去踩新世代刚置起来的加载标记
        if (gen === generation) llmConfigsLoaded.value = false
        throw err
      })
      .finally(() => {
        // 只释掉自己那一次:作废会把 inFlight 置空并开新请求,旧请求收尾时不能把它踩掉
        if (inFlight === request) inFlight = null
      })
    inFlight = request
  }
  return inFlight
}

/** 作废缓存(登出、以及模型清单增删改后调用),下次进入下拉所在页面会重新拉取 */
export function invalidateLlmConfigs(): void {
  // 先推进世代号再清状态:让所有在途请求在回调里自认出过时
  generation += 1
  // 连在途句柄一起释掉:否则刚登出就重新拉取的人(下一个登录者)会复用
  // 上一个账号那次请求,拿到被丢弃后的空列表,要退页重进才能刷新
  inFlight = null
  llmConfigsLoaded.value = false
  llmConfigs.value = []
}
