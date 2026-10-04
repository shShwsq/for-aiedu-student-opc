/**
 * 页内段落目录(锚点跳转 + scrollspy 高亮)
 *
 * 由 PracticeView 的左侧目录实现原样抽取,供两处复用:
 * - PracticeView:错题回顾 / 题库管理 / 历史记录
 * - 设置页二级目录(childMode='anchor',如练习设置的三段)
 *
 * 关键约定:
 * - 观察器的 root 必须是真正滚动的容器(不是 window),由 getRoot 提供
 * - gap(段首判定线)须与各段 CSS 的 scroll-margin-top 一致,否则高亮比滚动慢半拍
 * - 点击即刻置高亮,不等观察器回调(平滑滚动过程中避免闪烁)
 */
import { ref, type InjectionKey, type Ref } from 'vue'

/** SettingsLayout 提供给子路由面板的滚动根(右侧内容区 .settings-content) */
export const settingsScrollRootKey: InjectionKey<Ref<HTMLElement | null>> =
  Symbol('settingsScrollRoot')

/** hash('#id') 命中的段 id(未命中返回空串,由调用方决定回退策略) */
export function matchHashSectionId(
  ids: readonly string[],
  hash: string | undefined,
): string {
  if (!hash) return ''
  const id = hash.startsWith('#') ? hash.slice(1) : hash
  return ids.includes(id) ? id : ''
}

export interface SectionNavOptions {
  /** 段 id 顺序(与页面 DOM 顺序一致) */
  ids: readonly string[]
  /** 真正滚动的容器(overflow-y:auto 那一层) */
  getRoot: () => HTMLElement | null | undefined
  /** 段首判定线(px),须与 .anchor-section 的 scroll-margin-top 保持一致 */
  gap?: number
}

export interface SectionNav {
  /** 当前段 id(初始为第一段) */
  activeId: Ref<string>
  /** 点击目录:立即高亮 + 平滑滚动 */
  scrollToSection: (id: string) => void
  /** 段落进入 DOM 后调用(建观察器) */
  setup: () => void
  /** 段落离开 DOM / 组件卸载前调用(释放观察器) */
  teardown: () => void
}

export function useSectionNav(options: SectionNavOptions): SectionNav {
  const { ids, getRoot, gap = 16 } = options
  const activeId = ref<string>(ids[0] ?? '')
  let observer: IntersectionObserver | null = null

  function scrollToSection(id: string): void {
    activeId.value = id
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  /** 重算当前段:段首已到达判定线的段中取最靠下的那个(即正在阅读的段) */
  function updateActiveSection(): void {
    const root = getRoot()
    if (!root) return
    const rootTop = root.getBoundingClientRect().top
    let current: string | null = null
    let currentTop = Number.NEGATIVE_INFINITY
    for (const id of ids) {
      const el = document.getElementById(id)
      if (!el) continue
      const top = el.getBoundingClientRect().top - rootTop
      if (top > gap) continue
      if (top > currentTop) {
        currentTop = top
        current = id
      }
    }
    activeId.value = current ?? ids[0] ?? ''
  }

  function setup(): void {
    teardown()
    const targets = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null)
    if (targets.length === 0) return
    observer = new IntersectionObserver(updateActiveSection, {
      root: getRoot() ?? null,
      rootMargin: '0px 0px -70% 0px',
      threshold: 0,
    })
    for (const el of targets) observer.observe(el)
  }

  function teardown(): void {
    if (observer) {
      observer.disconnect()
      observer = null
    }
  }

  return { activeId, scrollToSection, setup, teardown }
}
