/**
 * 回归守卫:动态验证授权的"可退出"接线(静态扫描源码)。
 *
 * 本项目视图不做挂载测试(约定见 taskDetailViewSuggestionWiring.test.ts),
 * 这里只钉住三处"后端改了、前端不接就等于没改"的形状:
 *
 * 1. 授权等待现在有上限(后端 VERIFY_AUTH_TIMEOUT_SECONDS),「终止检查」也会立即
 *    唤醒等待线程。两种情况后端都清掉了 pending 动作并推 verify_action_resolved —
 *    前端不收尾的话,弹窗会挂在页面上等一个再也不会有人答复的决议。
 * 2. "同意,后续自动允许"(scope='task')是逐动作弹窗疲劳的唯一出路;不接这条链,
 *    用户只能选 direct(彻底没有人工把关)或点七八次同意。
 * 3. 决议提交合并成一条链(同意/不同意 × 仅本次/后续),避免四个分支各写一份。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const VIEW = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')
const DIALOG = readFileSync(resolve(process.cwd(), 'src/components/VerifyActionDialog.vue'), 'utf-8')
const STREAM = readFileSync(resolve(process.cwd(), 'src/api/stream.ts'), 'utf-8')

/** 取定位串之后的一段源码(找不到定位串就直接失败,免得断言空跑) */
function sliceFrom(src: string, anchor: string, chars = 800): string {
  const at = src.indexOf(anchor)
  expect(at, `源码中找不到定位串: ${anchor}`).toBeGreaterThanOrEqual(0)
  return src.slice(at, at + chars)
}

describe('验证动作授权:决议提交一条链', () => {
  it('submitVerifyResolution 带 scope 参数并按 approved/scope 区分三个按钮', () => {
    const fn = sliceFrom(VIEW, 'async function submitVerifyResolution(')
    expect(fn).toContain("scope: 'once' | 'task' = 'once'")
    expect(fn).toContain('action_id: actionId,')
    expect(fn).toContain('approved,')
    expect(fn).toContain('scope,')
    // 三个入口共用它(不再各写一份 fetch)
    expect(sliceFrom(VIEW, 'function handleApproveVerifyAction(')).toContain("submitVerifyResolution(actionId, true, 'once')")
    expect(sliceFrom(VIEW, 'function handleApproveAllVerifyAction(')).toContain("submitVerifyResolution(actionId, true, 'task')")
    expect(sliceFrom(VIEW, 'function handleRejectVerifyAction(')).toContain('submitVerifyResolution(actionId, false)')
  })

  it('弹窗绑定 approve-all,并给出"仅同意这一次"的对照措辞', () => {
    expect(VIEW).toContain('@approve-all="handleApproveAllVerifyAction"')
    expect(sliceFrom(DIALOG, "function handleApproveAll(")).toContain("emit('approve-all'")
    expect(DIALOG).toContain('仅同意这一次')
  })
})

describe('验证动作授权:失效事件要收起弹窗', () => {
  it('stream.ts 分派 verify_action_resolved', () => {
    expect(STREAM).toContain("case 'verify_action_resolved':")
    expect(STREAM).toContain('callbacks.onVerifyActionResolved?.')
  })

  it('详情页接了回调,且只收起对得上 action_id 的弹窗', () => {
    expect(VIEW).toContain('onVerifyActionResolved: (data: VerifyActionResolvedEventData) => {')
    const fn = sliceFrom(VIEW, 'function handleVerifyActionResolved(')
    expect(fn).toContain('verifyActionData.value?.action_id !== data.action_id')
    expect(fn).toContain('closeVerifyActionDialog()')
  })
})
