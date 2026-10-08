/**
 * Markdown 渲染工具
 *
 * 统一封装 marked.parse + DOMPurify.sanitize,供所有需要把 LLM/用户文本
 * 渲染为 HTML 的组件复用。
 *
 * 安全:输出 HTML 必须经 DOMPurify 净化后再注入 v-html。
 *      适用于不可信来源(LLM 输出、用户输入、agent 归纳的仓库内容)。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 同步解析(默认 async:false 返回 string,此处显式声明避免 TS 推断成 string | Promise)
marked.setOptions({ async: false, gfm: true, breaks: true })

/**
 * 把 Markdown 文本渲染为净化后的 HTML 字符串。
 *
 * - 空字符串/纯空白返回空串(调用方可用 v-if 控制显隐)
 * - 启用 GFM(表格、删除线、任务列表)和 breaks(单换行也成 <br>)
 * - 输出经 DOMPurify 净化,移除 <script>/on* 属性等 XSS 风险
 */
export function renderMarkdown(text: string | null | undefined): string {
  if (!text || !text.trim()) return ''
  const raw = marked.parse(text, { async: false }) as string
  return DOMPurify.sanitize(raw)
}

/**
 * 把纯文本转成可注入 v-html 的安全 HTML:只转义 & < >,不解析 markdown。
 *
 * 用途:流式增量期间的长文本(思考链可达数千字符)——逐 token 跑
 * marked+DOMPurify 会把渲染成本扣在每个增量上,而这段文本本来就是连续
 * 叙述,不需要排版。转义后注入,与 renderMarkdown 同样不接受原始 HTML。
 */
export function escapePlainText(text: string | null | undefined): string {
  if (!text) return ''
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}
