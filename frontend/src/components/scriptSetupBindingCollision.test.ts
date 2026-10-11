/**
 * 回归守卫:<script setup> 的顶层 import 绑定不得与 prop 同名
 *
 * 背景(2026-10 帮助气泡全站消失):HelpPopover 里 `import { size } from '@floating-ui/dom'`
 * 与 size prop 同名。<script setup> 会把顶层绑定(含 import)暴露给模板,同名时 import
 * 遮蔽 prop,于是模板里的 `help-btn-${size}` 拼成 `help-btn-function(options){…}`,
 * 尺寸类永不命中 → 按钮塌成 0x0 → 全站问号按钮集体看不见(控制台无任何报错)。
 * vue-tsc / vite build 都发现不了,所以用静态扫描兜。
 *
 * 只读源码、不渲染组件,与本项目「前端只测纯函数」的约定一致。
 */
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// 扫全部 src 下的 SFC(vitest 工作目录就是 frontend 根)
const SRC_ROOT = resolve(process.cwd(), 'src')

function listSfc(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = `${dir}/${name}`
    if (statSync(p).isDirectory()) {
      // 组件测试与日志等目录无需扫描
      if (name === 'node_modules' || name === '__tests__') continue
      listSfc(p, out)
    } else if (name.endsWith('.vue')) {
      out.push(p)
    }
  }
  return out
}

/** 取出 `import { a, b as c } from '...'` 落到作用域里的绑定名(别名取别名) */
function importedBindings(script: string): string[] {
  const names: string[] = []
  const re = /import\s+([^;\n]*?)\s+from\s+['"][^'"]+['"]/g
  let m: RegExpExecArray | null
  while ((m = re.exec(script))) {
    const clause = m[1]
    // `import type { X } from ...`:纯类型导入不产生运行时绑定,模板读不到,跳过
    if (/^type\s/.test(clause)) continue
    // 默认导入 / 命名空间导入:`import X from` / `import * as X from`
    const head = clause.replace(/\{[\s\S]*?\}/g, '').trim()
    const defaultMatch = head.match(/^([A-Za-z_$][\w$]*)/)
    if (defaultMatch && defaultMatch[1] !== 'type') names.push(defaultMatch[1])
    const nsMatch = clause.match(/\*\s+as\s+([A-Za-z_$][\w$]*)/)
    if (nsMatch) names.push(nsMatch[1])
    // 具名导入
    const braces = clause.match(/\{([\s\S]*?)\}/)
    if (braces) {
      for (const part of braces[1].split(',')) {
        const item = part.trim()
        if (!item || item.startsWith('//')) continue
        if (/^type\s/.test(item)) continue
        const seg = item.split(/\s+as\s+/)
        names.push((seg[1] ?? seg[0]).trim())
      }
    }
  }
  return names.filter(Boolean)
}

/** 取出 defineProps 声明的 prop 名(类型字面量写法与运行时对象写法都覆盖) */
function declaredProps(script: string): string[] {
  const names: string[] = []
  const typeMatch = script.match(/defineProps<\{([\s\S]*?)\}>\(\s*\)/)
  if (typeMatch) {
    for (const line of typeMatch[1].split('\n')) {
      // 只认「一行一个字段」的顶层声明:?: 或 : 前是标识符
      const m = line.match(/^\s*(?:readonly\s+)?([A-Za-z_$][\w$]*)\s*\??\s*:/)
      if (m) names.push(m[1])
    }
  }
  const runtimeMatch = script.match(/defineProps\(\s*\{([\s\S]*?)\n\s*\}\s*\)/)
  if (runtimeMatch) {
    for (const line of runtimeMatch[1].split('\n')) {
      const m = line.match(/^\s{2,}([A-Za-z_$][\w$]*)\s*:\s*\{/)
      if (m) names.push(m[1])
    }
  }
  return names
}

describe('script setup 绑定名与 prop 名不得冲突', () => {
  const files = listSfc(SRC_ROOT).filter((f) => /<script setup/.test(readFileSync(f, 'utf8')))

  it('扫描到若干 <script setup> 组件', () => {
    expect(existsSync(SRC_ROOT)).toBe(true)
    expect(files.length).toBeGreaterThan(0)
  })

  it('没有任何 import 绑定名与 prop 同名(同名会遮蔽 prop,模板读到的是函数)', () => {
    const conflicts: string[] = []
    for (const file of files) {
      const src = readFileSync(file, 'utf8')
      const script = src.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)?.[1] ?? ''
      const props = new Set(declaredProps(script))
      for (const binding of importedBindings(script)) {
        if (props.has(binding)) {
          const rel = file.replace(/\\/g, '/').replace(SRC_ROOT.replace(/\\/g, '/'), '')
          conflicts.push(`${rel}: import "${binding}" 遮蔽了同名 prop`)
        }
      }
    }
    expect(conflicts).toEqual([])
  })
})
