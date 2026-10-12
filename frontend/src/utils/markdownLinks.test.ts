/**
 * Markdown 预览链接分类/解析单元测试
 *
 * 回归目标:README 预览里点相对链接(如 [x](README.md))被浏览器按当前 SPA URL
 * (/tasks/{uuid})解析并整页跳到 /tasks/README.md,再用非 UUID 的 id 打后端 →
 * "Input should be a valid UUID"。链接点击须先拦下默认跳转,再按目标分流。
 *
 * 覆盖 classifyPreviewLink 的四类处置 + resolveWorkspaceLink 的路径口径。
 */
import { describe, expect, it } from 'vitest'

import { classifyPreviewLink, resolveWorkspaceLink } from './markdownLinks'

describe('resolveWorkspaceLink', () => {
  it('仓库根文件里的兄弟链接按原目录解析', () => {
    expect(resolveWorkspaceLink('README.md', 'docs/guide.md')).toBe('docs/guide.md')
  })

  it('子目录文件里的相对链接拼上其父目录', () => {
    expect(resolveWorkspaceLink('docs/guide.md', 'config.md')).toBe('docs/config.md')
  })

  it('以 / 开头视为仓库根相对', () => {
    expect(resolveWorkspaceLink('docs/guide.md', '/src/main.py')).toBe('src/main.py')
  })

  it('折叠 . 与 ..,越出根不产生非法路径', () => {
    expect(resolveWorkspaceLink('docs/sub/a.md', '../b.md')).toBe('docs/b.md')
    expect(resolveWorkspaceLink('docs/a.md', './b.md')).toBe('docs/b.md')
    expect(resolveWorkspaceLink('a.md', '../outside.md')).toBe('outside.md')
  })

  it('剥掉 #anchor / ?query 再解析', () => {
    expect(resolveWorkspaceLink('README.md', 'docs/guide.md#安装')).toBe('docs/guide.md')
    expect(resolveWorkspaceLink('README.md', 'a.md?raw=1')).toBe('a.md')
  })

  it('解 %xx 编码(空格等)', () => {
    expect(resolveWorkspaceLink('README.md', 'my%20doc.md')).toBe('my doc.md')
  })

  it('纯锚点解析不出路径,返回空串', () => {
    expect(resolveWorkspaceLink('README.md', '#section')).toBe('')
  })
})

describe('classifyPreviewLink', () => {
  it('安全外链判 external', () => {
    expect(classifyPreviewLink('README.md', 'https://example.com/x')).toEqual({
      kind: 'external',
      url: 'https://example.com/x',
    })
    expect(classifyPreviewLink('README.md', 'mailto:a@b.com').kind).toBe('external')
  })

  it('javascript: / data: 不作为可点目标(忽略)', () => {
    expect(classifyPreviewLink('README.md', 'javascript:alert(1)').kind).toBe('ignore')
    expect(classifyPreviewLink('README.md', 'data:image/png;base64,AAAA').kind).toBe('ignore')
  })

  it('页内锚点判 anchor 并给出 id', () => {
    expect(classifyPreviewLink('README.md', '#%E5%AE%89%E8%A3%85')).toEqual({
      kind: 'anchor',
      id: '安装',
    })
    expect(classifyPreviewLink('README.md', '#').kind).toBe('ignore')
  })

  it('工作区相对链接判 workspace 并给出解析后的路径', () => {
    expect(classifyPreviewLink('docs/guide.md', 'config.md')).toEqual({
      kind: 'workspace',
      path: 'docs/config.md',
    })
  })

  it('空 href 忽略', () => {
    expect(classifyPreviewLink('README.md', '').kind).toBe('ignore')
  })
})
