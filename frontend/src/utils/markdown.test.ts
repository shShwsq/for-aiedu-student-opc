/**
 * Markdown/纯文本注入工具的单元测试
 *
 * 覆盖回归点:思考框在流式期间走"只转义不排版"的快路径,输出仍要注入
 * v-html —— 转义漏一个字符就等于把模型输出当 HTML 执行(XSS 面)。
 */
import { describe, expect, it } from 'vitest'

import { escapePlainText, renderMarkdown } from './markdown'

describe('escapePlainText', () => {
  it('空值返回空串', () => {
    expect(escapePlainText('')).toBe('')
    expect(escapePlainText(null)).toBe('')
    expect(escapePlainText(undefined)).toBe('')
  })

  it('转义 & < >,原始 HTML 不会成为标签', () => {
    expect(escapePlainText('<script>alert(1)</script>')).toBe(
      '&lt;script&gt;alert(1)&lt;/script&gt;',
    )
    expect(escapePlainText('a && b < c > d')).toBe('a &amp;&amp; b &lt; c &gt; d')
  })

  it('换行与缩进原样保留(容器用 pre-wrap 呈现)', () => {
    expect(escapePlainText('  第一行\n第二行')).toBe('  第一行\n第二行')
  })

  it('引号不转义(注入文本节点而非属性)', () => {
    expect(escapePlainText('他说"继续"')).toBe('他说"继续"')
  })
})

describe('renderMarkdown', () => {
  it('纯空白返回空串(调用方按 v-if 控制显隐)', () => {
    expect(renderMarkdown('   \n ')).toBe('')
  })

  it('单换行按 breaks 成语句分隔(GFM + breaks 已开启)', () => {
    expect(renderMarkdown('a\nb')).toContain('<br')
  })
})
