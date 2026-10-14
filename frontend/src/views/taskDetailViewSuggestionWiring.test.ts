/**
 * 回归守卫:「概览」建议卡 + 审查项「可追问内容」在视图层的接线(静态扫描源码)
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
 *
 * 另加一组:审查项详情的「可追问内容」按钮(跟 reviewBucket.test.ts 的纯函数分工,
 * 这里只钉接线与标签形状)——共用同一把 diggingSuggestion 锁,否则抽屉里的追问
 * 与侧栏建议可以同时发两条,新一轮执行会互相踩。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// vitest 的工作目录是 frontend 根
const SRC = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')
const DRAWER_SRC = readFileSync(
  resolve(process.cwd(), 'src/components/DetailCardDrawer.vue'), 'utf-8',
)
const PANEL_SRC = readFileSync(
  resolve(process.cwd(), 'src/components/Agent2Panel.vue'), 'utf-8',
)

/** 取定位串之后的一段源码(找不到定位串就直接失败,免得断言空跑) */
function sliceFrom(anchor: string, chars = 900): string {
  return sliceIn(SRC, anchor, chars)
}

/** 同上,但指定要扫描哪个源文件(抽屉/面板的接线不在 TaskDetailView 里) */
function sliceIn(source: string, anchor: string, chars = 900): string {
  const at = source.indexOf(anchor)
  expect(at, `源码中找不到定位串: ${anchor}`).toBeGreaterThanOrEqual(0)
  return source.slice(at, at + chars)
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

describe('审查项「可追问内容」接线', () => {
  it('抽屉共用同一把 diggingSuggestion 锁,点击走 handleSuggestionDig', () => {
    const drawer = sliceFrom('<DetailCardDrawer', 400)
    expect(drawer).toContain(':digging="diggingSuggestion"')
    expect(drawer).toContain('@dig="handleSuggestionDig"')
    expect(DRAWER_SRC).toContain("(e: 'dig', text: string): void")
    expect(DRAWER_SRC).toContain('@click="emit(\'dig\', followUp)"')
    // 锁挂上时按钮置灰(避免同一句追问被连点两次)
    expect(sliceIn(DRAWER_SRC, 'const followUp = computed', 700)).toContain('props.digging')
  })

  it('字段与分档标签已改口(不再出现旧标签/旧列名)', () => {
    expect(DRAWER_SRC).toContain('待核实对象:')
    expect(DRAWER_SRC).toContain('审查结论:')
    expect(DRAWER_SRC).toContain('可追问内容:')
    expect(DRAWER_SRC).toContain('原始证据:')
    // 旧文案与旧列名不得残留
    expect(DRAWER_SRC).not.toContain('发现问题:')
    expect(DRAWER_SRC).not.toContain('被核实对象:')
    expect(DRAWER_SRC).not.toContain('item as ReviewItem).suggestion')
    expect(DRAWER_SRC).not.toContain('item as ReviewItem).description')
    // 卡片列表不再把英文枚举直铺给用户
    expect(SRC).toContain('originLabel(it.origin)')
    expect(SRC).not.toContain("<span class=\"origin-tag\" :title=\"it.origin\">{{ it.origin }}</span>")
    // 置信档位已从展示层去掉:reviewBadge 委派给 reviewItemBadge(待核实原因 / 仅 PoC 标记)
    expect(SRC).toContain('function reviewBadge')
    expect(sliceFrom('function reviewBadge', 300)).toContain('reviewItemBadge(it)')
  })

  it('计划卡不再显示"回填缺口",改叫未核实', () => {
    expect(PANEL_SRC).toContain('未核实 {{ reviewPlanTally.unverified }}')
    expect(PANEL_SRC).not.toContain('回填缺口')
    expect(PANEL_SRC).toContain('planTally(')
    expect(PANEL_SRC).toContain('originLabel(pi.origin)')
    // 计数与逐行状态都取自后端口径(前端不再复刻 target 子串匹配)
    expect(sliceIn(PANEL_SRC, 'function planStateOf', 300)).toContain('props.reviewSummary?.plan')
  })
})
