/**
 * 回归守卫:「概览」两组建议卡在视图层的接线(静态扫描源码)
 *
 * 修复前的用户可见症状(都只发生在「关闭检查助手」的单 agent 模式):
 * 1. 探索建议落库为 role=agent2,而 hasAgent2Activity 不分 type 一律算"有活动"
 *    → 本该隐藏的「核查过程」段凭空点亮,并把记录的原始 JSON 铺给用户;
 * 2. rail「概览」角标与「审查结果」段的跳转只数缺口组,探索组有内容时既恒显 0
 *    又断掉追问入口;
 * 3. 追问防重复锁只靠 review_status 的 watch 解,而该模式下后端全程不写这个
 *    字段(null → null 不触发)→ 点一次建议两组按钮一起锁死到刷新。
 *
 * 本项目前端只测纯函数(视图不挂载),以上三处都是视图内部状态接线,故按
 * components/scriptSetupBindingCollision.test.ts 的做法用静态扫描钉住源码形状。
 * 纯函数侧的覆盖见 utils/agent2Timeline.test.ts 与 utils/agent2Suggestions.test.ts。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// vitest 的工作目录是 frontend 根
const SRC = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')

/** 取锚点之后的一段源码(找不到锚点就直接失败,免得断言空跑) */
function sliceFrom(anchor: string, chars = 900): string {
  const at = SRC.indexOf(anchor)
  expect(at, `源码中找不到锚点: ${anchor}`).toBeGreaterThanOrEqual(0)
  return SRC.slice(at, at + chars)
}

describe('TaskDetailView 建议卡接线', () => {
  it('侧栏可见性排除建议类记录(核查过程段不为一条 JSON 点亮)', () => {
    expect(sliceFrom('const hasAgent2Activity = computed')).toContain('!isSuggestionConv(c)')
  })

  it('rail 概览角标按两组总数计(探索组有内容不再恒显 0)', () => {
    expect(sliceFrom('const sectionBadge = computed')).toContain('suggestionTotalCount.value')
    expect(sliceFrom('const suggestionTotalCount = computed')).toContain(
      'exploreSuggestions.value.length',
    )
    // 审查结果段的跳转同口径:只有探索建议时也不能断掉追问入口
    expect(SRC).toContain('{{ suggestionTotalCount }} 条建议追问')
  })

  it('发送成功即解除追问防重复锁(不依赖 review_status 变化)', () => {
    expect(sliceFrom('function handleMessageSent')).toContain('diggingSuggestion.value = null')
  })
})
