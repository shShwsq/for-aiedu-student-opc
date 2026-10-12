/**
 * 工具调用紧凑展示:把浏览型工具(读文件/搜索文件/列目录)的
 * tool_call + tool_result 配对压缩成一行摘要,替代原来的多层折叠卡片。
 *
 * 数据来源(后端已有,无需改动):
 * - tool_call content 首行:人类可读 intent,末尾带 [tool_name] 标签
 * - tool_result content:工具返回的结构化 JSON(read_file/find_files/search_code/list_files)
 */

import { stripWorkspaceRoot } from './workspacePath'

/** 工具项最小结构(TaskDetailView 的 DisplayItem 满足此接口) */
export interface ToolItem {
  id: string
  type?: string
  content?: string
  /** 仅 tool_result 有:对应 tool_call 项的 id(并行调用时精确配对;历史数据为空) */
  tool_call_id?: string | null
}

/** 紧凑化的工具集合:浏览型只读操作,一行摘要即可表达全部信息 */
export const COMPACT_TOOLS = new Set([
  'read_file',
  'find_files',
  'search_code',
  'list_files',
])

// ============================================================
// Bash/run_command 命令解析:只读命令白名单 → 单行摘要
//
// 来源:logs/acp 真实命令分布统计(cat 59/find 20/ls 12/grep 5/wc/head/tree),
// CLI 智能体(如 codex)无 Read 工具,全靠 bash 命令读文件。
// 安全策略:整条命令(含管道/链式)全部命中白名单才紧凑化,
// 出现写操作/未知命令/命令替换时回退原折叠展示。
// ============================================================

const READ_FILE_CMDS = new Set(['cat', 'head', 'tail', 'bat', 'less', 'more'])
const LIST_DIR_CMDS = new Set(['ls', 'll', 'dir', 'tree'])
const SEARCH_CMDS = new Set(['find', 'grep', 'rg', 'egrep', 'fgrep'])
/** 只读辅助命令(管道尾/链式成员安全) */
const AUX_CMDS = new Set([
  'wc', 'sort', 'uniq', 'cut', 'tr', 'file', 'stat', 'pwd', 'echo', 'true', 'cd',
])
const READONLY_CMDS = new Set([
  ...READ_FILE_CMDS, ...LIST_DIR_CMDS, ...SEARCH_CMDS, ...AUX_CMDS,
])

/** 从 tool_call content 提取命令文本:Bash 的 detail 是命令原文,
 * run_command(agent1)的 detail 是参数 JSON(含 command 字段) */
export function commandOf(call: ToolItem): string {
  const content = call.content || ''
  const idx = content.indexOf('\n')
  if (idx < 0) return ''
  const detail = content.slice(idx + 1).trim()
  if (detail.startsWith('{')) {
    try {
      const args = JSON.parse(detail)
      if (typeof args.command === 'string') return args.command
    } catch { /* 非 JSON 则按命令原文处理 */ }
  }
  return detail
}

/** 剥掉工作区根前缀(sandbox 的 /repos/<仓库>/ 或 local 的临时目录),路径更短更易读;
 *  规则见 workspacePath.stripWorkspaceRoot(顺带把 Windows 反斜杠归一成正斜杠) */
export function shortenPath(p: string): string {
  return stripWorkspaceRoot(p)
}

function stripQuotes(s: string): string {
  return s.replace(/^(['"])(.*)\1$/, '$2')
}

/** 取段的非 flag 参数(跳过 -n 等选项的值) */
function filesOf(args: string[]): string[] {
  const files: string[] = []
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (a.startsWith('-')) {
      if (a === '-n' || a === '-c') i++ // 跳过选项值
      continue
    }
    files.push(a)
  }
  return files
}

/** 提取 head/tail 的行数:-200 / -n 200 / -n200 */
function headTailLines(args: string[]): string | null {
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (/^-\d+$/.test(a)) return a.slice(1)
    if (a === '-n' && /^\d+$/.test(args[i + 1] || '')) return args[i + 1]
    const m = a.match(/^-n(\d+)$/)
    if (m) return m[1]
  }
  return null
}

interface CmdSeg { cmd: string; args: string[] }

/**
 * 解析命令为链式管道结构;含非只读内容时返回 null。
 * 处理:/bin/bash -lc 包装、cd 前缀、2>/dev/null 重定向、|| 回退分支(取首支)。
 */
function parseReadonlyChains(cmdRaw: string): CmdSeg[][] | null {
  let c = cmdRaw.trim()
  if (!c) return null
  // 危险特征直接拒绝:命令替换、sudo、xargs(可执行任意命令)
  if (/`|\$\(|\bsudo\b|\bxargs\b/.test(c)) return null
  // 剥掉 /bin/bash -lc '...' 包装(bash/zsh/sh,支持 -lc -c 等组合 flag)
  c = c.replace(/^(?:\/usr)?\/bin\/(?:ba|da|z)?sh\s+(?:-\w+\s+)+['"]?/, '')
  c = c.replace(/['"]$/, '')

  const chains = c.split(/\s*(?:&&|;)\s*/).filter(Boolean)
  if (!chains.length) return null

  const result: CmdSeg[][] = []
  for (const chain of chains) {
    // || 回退分支(如 || echo "not found")只看首支
    const main = chain.split(/\s*\|\|\s*/)[0]
    const pipes = main.split('|')
    const segs: CmdSeg[] = []
    for (const p of pipes) {
      // 剥 /dev/null 重定向与 fd 复制;剩余 > 视为写文件 → 拒绝
      const cleaned = p
        .replace(/\d*\s*>>?\s*\/dev\/null/g, '')
        .replace(/\d*>\s*&\d+/g, '')
      if (cleaned.includes('>')) return null
      const tokens = cleaned.trim().split(/\s+/).filter(Boolean).map(stripQuotes)
      // 跳过 env VAR=x 前缀
      let i = 0
      while (i < tokens.length && /^[\w]+=.*/.test(tokens[i])) i++
      const cmdToken = tokens[i]
      if (!cmdToken) return null
      const name = cmdToken.split('/').pop() || ''
      if (!READONLY_CMDS.has(name)) return null
      segs.push({ cmd: name, args: tokens.slice(i + 1) })
    }
    result.push(segs)
  }
  return result
}

/** 单个链式分支的摘要(无可展示内容如 cd/echo 时返回 null) */
function summarizeChain(segs: CmdSeg[]): string | null {
  const head = segs[0]
  const tail = segs.length > 1 ? segs[segs.length - 1] : null
  // 管道尾修饰:cat f | head -200 → 前 200 行;tail → 后 N 行
  let mod = ''
  if (tail && (tail.cmd === 'head' || tail.cmd === 'tail')) {
    const n = headTailLines(tail.args)
    if (n) mod = ` · ${tail.cmd === 'head' ? '前' : '后'} ${n} 行`
  }

  if (READ_FILE_CMDS.has(head.cmd)) {
    const files = filesOf(head.args)
    if (!files.length) return null
    if (head.cmd === 'head' || head.cmd === 'tail') {
      // head/tail 自身就是读文件:行数从自身参数取
      const n = headTailLines(head.args)
      const m = n ? ` · ${head.cmd === 'head' ? '前' : '后'} ${n} 行` : ''
      return files.length === 1
        ? `📖 阅读了 ${shortenPath(files[0])}${m}`
        : `📖 阅读了 ${files.length} 个文件${m}`
    }
    return files.length === 1
      ? `📖 阅读了 ${shortenPath(files[0])}${mod}`
      : `📖 阅读了 ${files.length} 个文件${mod}`
  }
  if (head.cmd === 'tree') {
    const dir = filesOf(head.args)[0]
    return `📂 查看目录树 ${dir ? shortenPath(dir) : '.'}`
  }
  if (LIST_DIR_CMDS.has(head.cmd)) {
    const dir = filesOf(head.args)[0]
    return `📂 查看目录 ${dir ? shortenPath(dir) : '.'}`
  }
  if (head.cmd === 'find') {
    const args = head.args
    const dir = args.find((a) => !a.startsWith('-'))
    const nameIdx = args.findIndex((a) => a === '-name' || a === '-iname')
    const pattern = nameIdx >= 0 ? stripQuotes(args[nameIdx + 1] || '') : ''
    return pattern
      ? `🔍 查找文件 ${pattern}`
      : `🔍 查找文件${dir ? `(${shortenPath(dir)})` : ''}`
  }
  if (SEARCH_CMDS.has(head.cmd)) {
    const nonFlag = filesOf(head.args)
    const pattern = (nonFlag[0] || '').slice(0, 40)
    const path = nonFlag[1]
    if (!pattern) return null
    return `🔍 搜索 ${pattern}${path ? ` · ${shortenPath(path)}` : ''}`
  }
  if (head.cmd === 'wc') {
    const files = filesOf(head.args)
    return files.length
      ? `📄 统计行数 ${shortenPath(files[0])}`
      : '📄 统计行数'
  }
  // cd/echo/pwd/sort 等辅助命令无独立展示价值
  return null
}

/**
 * 命令摘要:整条命令全部只读时返回单行文案,否则 null(保持原折叠展示)。
 * 多个链式分支(如 ls x && cat y)摘要用顿号连接,最多 3 段。
 */
export function summarizeCommand(cmdRaw: string): string | null {
  const chains = parseReadonlyChains(cmdRaw)
  if (!chains) return null
  const parts = chains
    .map(summarizeChain)
    .filter((s): s is string => !!s)
  if (!parts.length) return null
  if (parts.length > 3) return `${parts.slice(0, 3).join('、')} 等`
  return parts.join('、')
}

/** CLI 智能体原生浏览工具(Qoder Read/Grep/Glob):从调用参数 JSON 摘要,
 * 不依赖结果(结果是原始输出文本) */
const CLI_COMPACT_TOOLS = new Set(['Read', 'Grep', 'Glob'])

/** tool_call content 首行后的 detail 解析为 JSON(失败返回 null) */
function detailJsonOf(call: ToolItem): Record<string, unknown> | null {
  const content = call.content || ''
  const idx = content.indexOf('\n')
  if (idx < 0) return null
  const detail = content.slice(idx + 1).trim()
  if (!detail.startsWith('{')) return null
  try {
    const v = JSON.parse(detail)
    return v && typeof v === 'object' ? (v as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/**
 * Read/Grep/Glob 单行摘要:从调用参数 JSON 提取路径/模式。
 * 无有效参数(如旧数据只有 intent 没有 detail)时返回 null,回退原折叠展示。
 */
export function summarizeCliToolCall(call: ToolItem): string | null {
  const name = toolNameOf(call)
  const data = detailJsonOf(call)
  if (!data) return null
  if (name === 'Read') {
    const fp = typeof data.file_path === 'string' ? data.file_path : ''
    if (!fp) return null
    // limit/offset:读指定行范围时标注
    let mod = ''
    const limit = typeof data.limit === 'number' ? data.limit : null
    const offset = typeof data.offset === 'number' ? data.offset : 1
    if (limit !== null) {
      mod = offset > 1 ? ` · 第 ${offset}~${offset + limit} 行` : ` · 前 ${limit} 行`
    }
    return `📖 阅读了 ${shortenPath(fp)}${mod}`
  }
  if (name === 'Grep') {
    const pattern = typeof data.pattern === 'string' ? data.pattern.slice(0, 40) : ''
    if (!pattern) return null
    const path = typeof data.path === 'string' ? data.path : ''
    return `🔍 搜索 ${pattern}${path ? ` · ${shortenPath(path)}` : ''}`
  }
  if (name === 'Glob') {
    const pattern = typeof data.pattern === 'string' ? data.pattern : ''
    if (!pattern) return null
    return `🔍 查找文件 ${pattern}`
  }
  return null
}

/** 判断 tool_call 是否可紧凑化:内置浏览型工具、只读命令的 Bash/run_command,
 * 或参数完整的 CLI 原生 Read/Grep/Glob */
function isCompactCall(it: ToolItem): boolean {
  const name = toolNameOf(it)
  if (COMPACT_TOOLS.has(name)) return true
  if (CLI_COMPACT_TOOLS.has(name)) return !!summarizeCliToolCall(it)
  if (name === 'Bash' || name === 'run_command') {
    return !!summarizeCommand(commandOf(it))
  }
  return false
}

/** 配对后的渲染段:
 * - compact:紧凑工具对(单行摘要)
 * - agent:子智能体调用对(轨迹报告卡片)
 * - toolpair:普通工具对(调用+结果整体一张折叠卡)
 * - plain:落单的 tool_result 等,保持原渲染兜底。
 * 泛型 T 保留调用方传入的具体项类型(如 TaskDetailView 的 DisplayItem) */
export type ToolSegment<T extends ToolItem = ToolItem> =
  | { kind: 'compact' | 'agent' | 'toolpair'; call: T; result: T | null }
  | { kind: 'plain'; items: T[] }

/** 安全解析 JSON,失败返回 null(tool_result 可能是错误消息文本) */
function tryParseJson(content: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(content)
    return v && typeof v === 'object' ? (v as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** 从 tool_call content 提取工具名(匹配首行末尾的 [tool_name] 标签) */
export function toolNameOf(item: ToolItem): string {
  const firstLine = (item.content || '').split('\n', 1)[0]
  const m = firstLine.match(/\[(\w+)\]$/)
  return m ? m[1] : ''
}

/** intent 首行(剥掉末尾 [tool_name] 标签) */
function intentOf(item: ToolItem): string {
  const firstLine = (item.content || '').split('\n', 1)[0]
  return firstLine.replace(/\s*\[\w+\]$/, '') || toolNameOf(item) || '工具调用'
}

/**
 * 生成紧凑工具的单行摘要文案。
 *
 * - result 为 null:执行中(agent1 落库 tool_call 后、结果返回前的短暂窗口)
 * - result 非 JSON:执行失败,直接透出错误原因
 * - 正常:按工具类型提取计数/路径,如「阅读了 src/main.py · 356 行」
 */
export function buildToolSummary(call: ToolItem, result: ToolItem | null): string {
  const name = toolNameOf(call)
  const intent = intentOf(call)

  // Bash/run_command:按命令内容摘要(结果是原始输出文本,非 JSON)
  if (name === 'Bash' || name === 'run_command') {
    const s = summarizeCommand(commandOf(call))
    if (!s) return intent
    if (!result || !result.content) return `${s} ...`
    return s
  }

  // CLI 原生 Read/Grep/Glob:按调用参数摘要(结果同样是原始输出文本)
  if (CLI_COMPACT_TOOLS.has(name)) {
    const s = summarizeCliToolCall(call)
    if (!s) return intent
    if (!result || !result.content) return `${s} ...`
    return s
  }

  if (!result || !result.content) return `${intent} ...`

  const data = tryParseJson(result.content)
  if (!data) {
    const msg = result.content.replace(/^工具执行失败:\s*/, '')
    return `❌ ${intent} · ${msg.split('\n', 1)[0]}`
  }

  switch (name) {
    case 'read_file': {
      const path = (data.path as string) || '?'
      const lines = typeof data.total_lines === 'number' ? data.total_lines : null
      const suffix = data.truncated ? '(截断)' : ''
      const linesTxt = lines !== null ? ` · ${lines} 行` : ''
      return `📖 阅读了 ${path}${linesTxt}${suffix}`
    }
    case 'find_files': {
      const pattern = (data.pattern as string) || ''
      const n = typeof data.total === 'number' ? data.total : ((data.files as unknown[]) ?? []).length
      return `🔍 搜索到 ${n} 个文件${pattern ? `(${pattern})` : ''}`
    }
    case 'search_code': {
      const n = typeof data.total_matches === 'number'
        ? data.total_matches
        : ((data.matches as unknown[]) ?? []).length
      return `🔍 搜索到 ${n} 处代码匹配`
    }
    case 'list_files': {
      const path = (data.path as string) || '根目录'
      const n = typeof data.total === 'number' ? data.total : ((data.entries as unknown[]) ?? []).length
      return `📂 查看目录 ${path} · ${n} 项`
    }
    default:
      return intent
  }
}

/** 紧凑工具中「阅读文件」的跳转目标:工作区相对路径 + 摘要拆分(路径文本前后缀) */
export interface ToolFileTarget {
  /** 工作区相对路径(用于跳转 openTaskFile) */
  path: string
  /** 摘要中展示的路径文本 */
  display: string
  /** 摘要 = prefix + display + suffix(模板把 display 包成可点击链接) */
  prefix: string
  suffix: string
}

/**
 * 从紧凑工具对提取「阅读文件」跳转目标;非读文件工具/多文件返回 null。
 * - read_file:从结果 JSON 取 path;执行中(无结果)时从 intent「读取文件 <路径>」解析
 * - CLI Read:从调用参数 JSON 取 file_path
 * - Bash/run_command:仅支持 cat/head 等读文件命令的单文件形式
 */
export function toolFileTargetOf(
  call: ToolItem,
  result: ToolItem | null,
): ToolFileTarget | null {
  const name = toolNameOf(call)
  let rawPath = ''
  if (name === 'read_file') {
    const data = result?.content ? tryParseJson(result.content) : null
    rawPath = (data?.path as string) || ''
    if (!rawPath) {
      const m = intentOf(call).match(/^读取文件\s+(\S+)/)
      rawPath = m ? m[1] : ''
    }
  } else if (name === 'Read') {
    const data = detailJsonOf(call)
    rawPath = typeof data?.file_path === 'string' ? data.file_path : ''
  } else if (name === 'Bash' || name === 'run_command') {
    const chains = parseReadonlyChains(commandOf(call))
    if (chains?.length === 1) {
      const head = chains[0][0]
      if (head && READ_FILE_CMDS.has(head.cmd)) {
        const files = filesOf(head.args)
        if (files.length === 1) rawPath = files[0]
      }
    }
  }
  if (!rawPath || rawPath === '?') return null

  // read_file 结果路径已是仓库相对路径(摘要原样展示);
  // CLI/Bash 给的是工作区绝对路径,展示与跳转都剥掉工作区根前缀。
  // 摘要与 path 必须出自同一个 shortenPath 调用:下面的 indexOf 匹配依赖两者文本一致
  const display = name === 'read_file' ? rawPath : shortenPath(rawPath)
  const summary = buildToolSummary(call, result)
  const idx = summary.indexOf(display)
  if (idx < 0) return null // 摘要中未出现路径文本,兜底不生成跳转
  return {
    path: shortenPath(rawPath),
    display,
    prefix: summary.slice(0, idx),
    suffix: summary.slice(idx + display.length),
  }
}

/**
 * 把迭代内的 tool_call/tool_result 列表转成渲染段序列:
 * 优先按 result.tool_call_id === call.id 精确配对(并行调用时 result 按完成顺序落库,
 * 不再紧跟 call);tool_call_id 缺失(历史数据)或无匹配时回退相邻配对;
 * 按调用类型分为 compact(浏览型单行摘要)/agent(子智能体)/toolpair(普通工具)三种卡片段;
 * 尚未到达的 result 为 null,落单的 tool_result 归入 plain 兜底。
 */
export function buildToolSegments<T extends ToolItem>(items: T[]): ToolSegment<T>[] {
  const segments: ToolSegment<T>[] = []
  let plain: T[] = []
  // 已被某个 call 配走的 result id(含非相邻匹配,避免重复渲染)
  const consumed = new Set<string>()
  const flush = () => {
    if (plain.length) {
      segments.push({ kind: 'plain', items: plain })
      plain = []
    }
  }
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    if (consumed.has(it.id)) continue  // 已被前面 call 非相邻配走的 result
    if (it.type !== 'tool_call') {
      // 落单 result(无 call 可配)兜底原渲染
      plain.push(it)
      continue
    }
    flush()
    const kind: 'compact' | 'agent' | 'toolpair' = isCompactCall(it)
      ? 'compact'
      : toolNameOf(it) === 'Agent'
        ? 'agent'
        : 'toolpair'
    let result: T | null = null
    const next = items[i + 1]
    if (next && next.type === 'tool_result' && !consumed.has(next.id)) {
      // 相邻 result:无 tool_call_id(历史数据)直接配;有的话必须指向本 call
      // (并行场景下相邻 result 可能属于先完成的另一个 call,不能错配)
      if (!next.tool_call_id || next.tool_call_id === it.id) {
        result = next
        consumed.add(next.id)
      }
    }
    if (!result) {
      // 向后找第一个 tool_call_id 指向本 call 的 result(并行完成顺序乱序场景)
      for (let j = i + 1; j < items.length; j++) {
        const cand = items[j]
        if (cand.type === 'tool_result' && cand.tool_call_id === it.id && !consumed.has(cand.id)) {
          result = cand
          consumed.add(cand.id)
          break
        }
      }
    }
    segments.push({ kind, call: it, result })
  }
  flush()
  return segments
}

// ============================================================
// 子智能体(Agent)轨迹解析
// ============================================================

/** 子智能体轨迹(Agent tool_result 的 rawOutput)结构化解析结果 */
export interface AgentTrace {
  /** 子智能体类型(A 族 actual_subagent_type,如 explore);B 族无头时为 null */
  subType: string | null
  /** 执行状态(A 族 status,如 completed);B 族为 null */
  status: string | null
  /** 内部思考 <think> 块内容(无则空串) */
  think: string
  /** 报告正文(已剥元信息头/[summary]/think 标记),Markdown */
  body: string
  /** 是否只拿到"后台已启动"回执(异步派生,结果尚未回报) */
  asyncPending: boolean
  /** 回执里的子任务 id(Qoder 形如 ageneral-purpose-…;无则 null) */
  agentId: string | null
}

/** 异步派生回执标记(Qoder 的 Agent 工具 fire-and-forget) */
const ASYNC_LAUNCH_RE = /Async agent launched|working in the background/i

/** 回执形状下的固定正文:不把"已启动"回执当报告渲染 */
export const AGENT_ASYNC_PENDING_BODY = '后台子任务已派生,结果尚未回报'

/**
 * 解析子智能体输出轨迹。
 *
 * 三种形状(logs/acp 实测):
 * - A 族(Kimi):开头若干 `key: value` 元信息行(agent_id/actual_subagent_type/status),
 *   其后 [summary] 标记、可选 <think> 块,再后是 Markdown 报告正文
 * - B 族(Qoder/qwen):无元信息头,直接是 Markdown 报告正文
 * - C 族(Qoder 异步派生回执):"Async agent launched successfully…agentId: xxx…
 *   working in the background" —— 它不是结论,必须标成"未回报"而不是"已完成"
 *   (回归:旧版只认 A 族的 status 头,回执里没有 status 行就落到"已完成"分支,
 *   卡片谎报完成而正文只是启动回执)
 */
export function parseAgentTrace(content: string): AgentTrace {
  let subType: string | null = null
  let agentId: string | null = null
  let status: string | null = null

  const asyncPending = ASYNC_LAUNCH_RE.test(content)

  // A 族元信息头 / C 族回执里的 agentId:仅开头连续的 key: value 行(遇到首个不匹配行即止)
  const lines = content.split('\n')
  let idx = 0
  while (idx < lines.length) {
    const line = lines[idx]
    const m = line.match(/^(agent_id|agentId|actual_subagent_type|status):\s*(\S.*)$/)
    if (!m) break
    if (m[1] === 'actual_subagent_type') subType = m[2].trim()
    else if (m[1] === 'status') status = m[2].trim()
    else agentId = m[2].trim().split(/\s+/)[0] // agent_id / agentId(剥尾随括号说明)
    idx++
  }
  // C 族回执的 agentId 不在开头(首行是 "Async agent launched successfully.",会提前
  //  break 掉上面的连续头解析),因此单独全文提一次
  if (!agentId) {
    const idMatch = content.match(/\bagentId:\s*([A-Za-z0-9_.-]+)/)
    if (idMatch) agentId = idMatch[1]
  }
  let rest = lines.slice(idx).join('\n')

  // 剥 [summary] 标记行
  rest = rest.replace(/^\s*\[summary\]\s*$/m, '')

  // 提取 <think> 块(内部思考,与报告正文分离)
  let think = ''
  const thinkMatch = rest.match(/<think>[\s\S]*?<\/think>/)
  if (thinkMatch) {
    think = thinkMatch[0].replace(/^<think>/, '').replace(/<\/think>$/, '').trim()
    rest = rest.replace(thinkMatch[0], '')
  }

  return {
    subType,
    status,
    think,
    // 回执形状:正文固定为诚实说明(原始回执留在调用参数/详情块可展开查看)
    body: asyncPending ? AGENT_ASYNC_PENDING_BODY : rest.trim(),
    asyncPending,
    agentId,
  }
}
