/**
 * buildToolSegments / buildToolSummary / parseAgentTrace 单元测试
 *
 * 覆盖工具段的配对与渲染摘要:
 * 1. 配对:compact/agent/toolpair 三类工具段与紧邻 result 配对,落单项进 plain 兜底
 * 2. 摘要:各工具的计数/路径提取、执行中、执行失败兜底
 * 3. Bash/run_command 命令解析:只读命令白名单摘要、危险命令拒绝
 * 4. 子智能体轨迹解析:A 族元信息头/think 块/B 族无头格式/C 族异步派生回执
 */
import { describe, expect, it } from 'vitest'

import {
  buildToolSegments,
  buildToolSummary,
  commandOf,
  AGENT_ASYNC_PENDING_BODY,
  parseAgentTrace,
  shortenPath,
  summarizeCliToolCall,
  summarizeCommand,
  toolFileTargetOf,
  toolNameOf,
  type ToolItem,
} from './toolSummary'

function call(id: string, intent: string): ToolItem {
  return { id, type: 'tool_call', content: intent }
}

function result(id: string, content: string, toolCallId?: string): ToolItem {
  return { id, type: 'tool_result', content, tool_call_id: toolCallId ?? null }
}

describe('toolNameOf', () => {
  it('从首行末尾提取 [tool_name] 标签', () => {
    expect(toolNameOf(call('1', '读取文件 src/main.py [read_file]\n{"a":1}')))
      .toBe('read_file')
  })

  it('无标签时返回空串', () => {
    expect(toolNameOf(call('1', '旧数据无标签'))).toBe('')
  })

  it('只匹配首行,忽略后续行的方括号', () => {
    expect(toolNameOf(call('1', '执行命令: ls [run_command]\n{"pattern":"[a-z]"}')))
      .toBe('run_command')
  })
})

describe('buildToolSummary', () => {
  const readCall = call('1', '读取文件 src/main.py [read_file]')

  it('read_file:路径 + 总行数', () => {
    const r = result('2', JSON.stringify({
      path: 'src/main.py', content: '...', total_lines: 356, truncated: false,
    }))
    expect(buildToolSummary(readCall, r)).toBe('📖 阅读了 src/main.py · 356 行')
  })

  it('read_file:截断时追加标记', () => {
    const r = result('2', JSON.stringify({
      path: 'big.py', content: '...', total_lines: 9999, truncated: true,
    }))
    expect(buildToolSummary(readCall, r)).toBe('📖 阅读了 big.py · 9999 行(截断)')
  })

  it('find_files:文件数 + pattern', () => {
    const c = call('1', '查找文件: **/*.py [find_files]')
    const r = result('2', JSON.stringify({
      pattern: '**/*.py', files: ['a.py', 'b.py'], total: 12, truncated: false, offset: 0,
    }))
    expect(buildToolSummary(c, r)).toBe('🔍 搜索到 12 个文件(**/*.py)')
  })

  it('search_code:匹配数', () => {
    const c = call('1', '搜索代码: TODO [search_code]')
    const r = result('2', JSON.stringify({
      matches: [{ file: 'a.py', line: 1, content: 'TODO' }],
      total_matches: 8, truncated: false,
    }))
    expect(buildToolSummary(c, r)).toBe('🔍 搜索到 8 处代码匹配')
  })

  it('list_files:目录 + 条目数', () => {
    const c = call('1', '查看目录结构: src [list_files]')
    const r = result('2', JSON.stringify({
      path: 'src/', entries: [{ name: 'main.py', type: 'file', size: 1 }],
      total: 45, truncated: false,
    }))
    expect(buildToolSummary(c, r)).toBe('📂 查看目录 src/ · 45 项')
  })

  it('result 未到达:显示意图 + 省略号', () => {
    expect(buildToolSummary(readCall, null)).toBe('读取文件 src/main.py ...')
  })

  it('执行失败(非 JSON):透出错误原因', () => {
    const r = result('2', '工具执行失败: 文件不存在: nope.py')
    expect(buildToolSummary(readCall, r)).toBe('❌ 读取文件 src/main.py · 文件不存在: nope.py')
  })

  it('未知工具名:回退显示 intent', () => {
    const c = call('1', '调用 custom_tool [custom_tool]')
    const r = result('2', JSON.stringify({ ok: true }))
    expect(buildToolSummary(c, r)).toBe('调用 custom_tool')
  })
})

describe('buildToolSegments', () => {
  it('紧凑 call+result 配对成 compact 段', () => {
    const items = [
      call('c1', '读取文件 a.py [read_file]'),
      result('r1', '{"path":"a.py","total_lines":1}'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ kind: 'compact', call: { id: 'c1' }, result: { id: 'r1' } })
  })

  it('紧凑 call 无 result(执行中):result 为 null', () => {
    const segs = buildToolSegments([call('c1', '读取文件 a.py [read_file]')])
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ kind: 'compact', result: null })
  })

  it('非紧凑工具配对成 toolpair 段且保持原顺序', () => {
    const items = [
      call('c1', '写入文件 b.py [write_file]'),
      result('r1', 'ok'),
      call('c2', '执行命令: pytest [run_command]\n{"command": "pytest"}'),
      result('r2', 'passed'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(2)
    expect(segs[0]).toMatchObject({ kind: 'toolpair', call: { id: 'c1' }, result: { id: 'r1' } })
    expect(segs[1]).toMatchObject({ kind: 'toolpair', call: { id: 'c2' }, result: { id: 'r2' } })
  })

  it('Agent 子任务配对成 agent 段', () => {
    const items = [
      call('c1', '子任务: 探索项目架构 [Agent]\n{"prompt": "探索"}'),
      result('r1', 'agent_id: agent-0\nstatus: completed'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ kind: 'agent', call: { id: 'c1' }, result: { id: 'r1' } })
  })

  it('Agent 执行中(无 result):result 为 null', () => {
    const segs = buildToolSegments([call('c1', '子任务: x [Agent]')])
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ kind: 'agent', result: null })
  })

  it('混合场景:compact/toolpair/agent 交替,顺序不乱', () => {
    const items = [
      call('c1', '读取文件 a.py [read_file]'),
      result('r1', '{}'),
      call('c2', '写入文件 b.py [write_file]'),
      result('r2', 'ok'),
      call('c3', '搜索代码: TODO [search_code]'),
      result('r3', '{}'),
      call('c4', '子任务: 探索架构 [Agent]'),
      result('r4', '## 报告'),
    ]
    const segs = buildToolSegments(items)
    expect(segs.map((s) => s.kind)).toEqual(['compact', 'toolpair', 'compact', 'agent'])
  })

  it('落单的 tool_result 归入 plain 兜底', () => {
    const segs = buildToolSegments([result('r1', '孤儿结果')])
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('plain')
  })

  it('并行调用:call 集中落库、result 按完成顺序乱序,按 tool_call_id 精确配对', () => {
    const items = [
      call('c1', '写入文件 a.py [write_file]'),
      call('c2', '执行命令: pytest [run_command]\n{"command": "pytest"}'),
      // c2 先完成,其 result 紧邻 c2 但属于 c2;c1 的 result 最后才到
      result('r2', 'passed', 'c2'),
      result('r1', 'ok', 'c1'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(2)
    expect(segs[0]).toMatchObject({ kind: 'toolpair', call: { id: 'c1' }, result: { id: 'r1' } })
    expect(segs[1]).toMatchObject({ kind: 'toolpair', call: { id: 'c2' }, result: { id: 'r2' } })
  })

  it('并行调用:相邻 result 属于另一个 call 时不错配', () => {
    const items = [
      call('c1', '写入文件 a.py [write_file]'),
      call('c2', '写入文件 b.py [write_file]'),
      result('r2', '结果B', 'c2'),  // 紧邻 c2,但 c1 也会先扫到它 —— 不能配给 c1
      result('r1', '结果A', 'c1'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(2)
    expect(segs[0]).toMatchObject({ call: { id: 'c1' }, result: { id: 'r1' } })
    expect(segs[1]).toMatchObject({ call: { id: 'c2' }, result: { id: 'r2' } })
  })

  it('并行调用:部分 result 未到达,已到 result 仍配到正确 call', () => {
    const items = [
      call('c1', '写入文件 a.py [write_file]'),
      call('c2', '写入文件 b.py [write_file]'),
      result('r2', 'ok', 'c2'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(2)
    expect(segs[0]).toMatchObject({ call: { id: 'c1' }, result: null })
    expect(segs[1]).toMatchObject({ call: { id: 'c2' }, result: { id: 'r2' } })
  })

  it('历史数据(无 tool_call_id)仍按相邻配对兜底', () => {
    const items = [
      call('c1', '写入文件 a.py [write_file]'),
      result('r1', 'ok'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ call: { id: 'c1' }, result: { id: 'r1' } })
  })
})

describe('parseAgentTrace(子智能体轨迹)', () => {
  it('A 族(Kimi):元信息头 + [summary] + think + 报告正文', () => {
    const trace = parseAgentTrace([
      'agent_id: agent-0',
      'actual_subagent_type: explore',
      'status: completed',
      '',
      '[summary]',
      '<think>',
      '我需要先了解项目结构',
      '然后阅读核心模块代码',
      '</think>',
      '',
      '## 项目架构分析',
      '',
      '核心入口位于 `src/main.py`。',
    ].join('\n'))
    expect(trace.subType).toBe('explore')
    expect(trace.status).toBe('completed')
    expect(trace.think).toBe('我需要先了解项目结构\n然后阅读核心模块代码')
    expect(trace.body).toBe('## 项目架构分析\n\n核心入口位于 `src/main.py`。')
  })

  it('A 族无 think 块:think 为空串,body 完整保留', () => {
    const trace = parseAgentTrace([
      'agent_id: agent-1',
      'actual_subagent_type: verifier',
      'status: completed',
      '',
      '[summary]',
      '验证通过,共 3 处修复。',
    ].join('\n'))
    expect(trace.subType).toBe('verifier')
    expect(trace.think).toBe('')
    expect(trace.body).toBe('验证通过,共 3 处修复。')
  })

  it('B 族(Qoder/qwen):无元信息头,直接 Markdown 正文', () => {
    const trace = parseAgentTrace('## 调查报告\n\n- 文件 a.py 存在风险\n- 文件 b.py 正常')
    expect(trace.subType).toBeNull()
    expect(trace.status).toBeNull()
    expect(trace.think).toBe('')
    expect(trace.body).toBe('## 调查报告\n\n- 文件 a.py 存在风险\n- 文件 b.py 正常')
  })

  it('B 族正文中含 key: value 样式内容时不误判为元信息头', () => {
    // 元信息头仅识别开头连续的指定 key 行;Markdown 标题行直接终止扫描
    const trace = parseAgentTrace('# 报告\n\nstatus: 该字段表示任务状态\n')
    expect(trace.status).toBeNull()
    expect(trace.body).toContain('status: 该字段表示任务状态')
  })

  it('status 非 completed 时保留(前端标题展示用)', () => {
    const trace = parseAgentTrace('agent_id: agent-0\nstatus: interrupted\n\n[summary]\n执行被中断。')
    expect(trace.status).toBe('interrupted')
    expect(trace.body).toBe('执行被中断。')
  })

  it('空输入:全部字段为空/空串', () => {
    const trace = parseAgentTrace('')
    expect(trace).toEqual({
      subType: null, status: null, think: '', body: '', asyncPending: false, agentId: null,
    })
  })

  // C 族:Qoder 异步派生回执。回归背景(任务 f4ff8649):回执里没有 status 行,
  // 旧版把它当普通报告正文渲染,卡片同时显示"已完成"—— 用户以为子任务已交结果。
  it('C 族异步回执:asyncPending=true 且 body 不是回执原文', () => {
    const ack = [
      'Async agent launched successfully.',
      'agentId: ageneral-purpose-5f77f8122aee3358 (internal ID - do not mention to user.)',
      'The agent is working in the background. You will be notified automatically when it completes.',
    ].join('\n')
    const trace = parseAgentTrace(ack)
    expect(trace.asyncPending).toBe(true)
    expect(trace.agentId).toBe('ageneral-purpose-5f77f8122aee3358')
    // 不把"已在后台启动"当结论渲染
    expect(trace.body).toBe(AGENT_ASYNC_PENDING_BODY)
    expect(trace.body).not.toContain('Async agent launched')
    // 无 status 行也不能被当成 completed(status 仍为 null,由调用侧走 asyncPending 分支)
    expect(trace.status).toBeNull()
  })

  it('C 族回执只靠英文标记判定,正文里出现"background"的报告不误判', () => {
    const trace = parseAgentTrace('## Audit\n\n- the worker runs in a threadpool in the background.')
    expect(trace.asyncPending).toBe(false)
    expect(trace.body).toContain('threadpool')
  })

  it('异步回执无 agentId 时仍判 asyncPending(agentId 仅标注用)', () => {
    const trace = parseAgentTrace('Async agent launched successfully. It is working in the background.')
    expect(trace.asyncPending).toBe(true)
    expect(trace.agentId).toBeNull()
  })

  it('A 族头里带 agent_id 时抽取到 agentId(不干扰原有字段)', () => {
    const trace = parseAgentTrace('agent_id: agent-0\nactual_subagent_type: explore\n\n[summary]\n报告正文')
    expect(trace.agentId).toBe('agent-0')
    expect(trace.subType).toBe('explore')
    expect(trace.asyncPending).toBe(false)
  })
})

describe('commandOf', () => {
  it('Bash:首行后全部是命令原文', () => {
    const c = call('1', "执行: cat /x/f.py [Bash]\n/bin/bash -lc 'cat /x/f.py'")
    expect(commandOf(c)).toBe("/bin/bash -lc 'cat /x/f.py'")
  })

  it('run_command:从参数 JSON 提取 command 字段', () => {
    const c = call('1', '执行命令: pytest [run_command]\n{"command": "pytest -x", "timeout": 60}')
    expect(commandOf(c)).toBe('pytest -x')
  })

  it('无 detail 时返回空串', () => {
    expect(commandOf(call('1', '执行: ls [Bash]'))).toBe('')
  })
})

describe('shortenPath', () => {
  it('剥掉 /repos/<仓库名>/ 前缀', () => {
    expect(shortenPath('/home/user/repos/openclaw-manager/services/app.py'))
      .toBe('services/app.py')
  })

  it('剥掉 local 模式宿主机临时目录前缀(Windows 反斜杠)', () => {
    expect(
      shortenPath(
        'C:\\Users\\njwjx\\AppData\\Local\\Temp\\sandbox_local_5_bgvr6o\\myrepo\\app.py',
      ),
    ).toBe('app.py')
  })

  it('无仓库前缀时保持原样', () => {
    expect(shortenPath('src/main.py')).toBe('src/main.py')
  })
})

describe('summarizeCommand(日志真实命令形态)', () => {
  it('cat 单文件(bash -lc 包装)', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'cat /home/user/repos/openclaw-manager/services/manager-web/metadata_store.py'",
    )).toBe('📖 阅读了 services/manager-web/metadata_store.py')
  })

  it('cat | head -N → 前 N 行', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'cat /home/user/repos/proj/services/manager-web/app.py | head -200'",
    )).toBe('📖 阅读了 services/manager-web/app.py · 前 200 行')
  })

  it('cat 多文件', () => {
    expect(summarizeCommand('cat a.py b.py c.py')).toBe('📖 阅读了 3 个文件')
  })

  it('cat 带 || echo 回退分支', () => {
    expect(summarizeCommand(
      '/bin/bash -lc \'cat /home/user/.agent_memory/project_memory.md 2>/dev/null || echo "Memory file not found"\'',
    )).toBe('📖 阅读了 /home/user/.agent_memory/project_memory.md')
  })

  it('head -n N 直接读文件', () => {
    expect(summarizeCommand('head -n 50 src/main.py')).toBe('📖 阅读了 src/main.py · 前 50 行')
  })

  it('tail -20 读文件尾部', () => {
    expect(summarizeCommand('tail -20 app.log')).toBe('📖 阅读了 app.log · 后 20 行')
  })

  it('ls -la 列目录', () => {
    expect(summarizeCommand(
      "ls -la /home/user/repos/openclaw-manager/services/",
    )).toBe('📂 查看目录 services/')
  })

  it('tree 列目录树', () => {
    expect(summarizeCommand('tree src')).toBe('📂 查看目录树 src')
  })

  it('find -name 查找文件', () => {
    expect(summarizeCommand(
      '/bin/bash -lc \'find /home/user/repos/openclaw-manager -type f -name "*.py" | head -50\'',
    )).toBe('🔍 查找文件 *.py')
  })

  it('grep 搜索内容与路径', () => {
    expect(summarizeCommand('grep -rn "TODO" src/')).toBe('🔍 搜索 TODO · src/')
  })

  it('wc -l 统计行数', () => {
    expect(summarizeCommand('wc -l src/main.py')).toBe('📄 统计行数 src/main.py')
  })

  it('链式命令 cd + ls && cat | head', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'cd /home/user/repos/proj && ls -la *.md && cat README.md | head -100'",
    )).toBe('📂 查看目录 *.md、📖 阅读了 README.md · 前 100 行')
  })

  it('非只读命令拒绝:rm', () => {
    expect(summarizeCommand('rm -rf /tmp/x')).toBeNull()
  })

  it('写重定向拒绝:cat f > out', () => {
    expect(summarizeCommand('cat a.py > out.txt')).toBeNull()
  })

  it('命令替换拒绝:$( )', () => {
    expect(summarizeCommand('echo $(cat /etc/passwd)')).toBeNull()
  })

  it('xargs 拒绝(可执行任意命令)', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'find /home/user/repos/proj -name \"*.py\" -type f | xargs wc -l | tail -20'",
    )).toBeNull()
  })

  it('sudo 拒绝', () => {
    expect(summarizeCommand('sudo cat /etc/shadow')).toBeNull()
  })

  it('未知命令拒绝:pip install', () => {
    expect(summarizeCommand('pip install requests')).toBeNull()
  })
})

describe('CLI 原生 Read/Grep/Glob 紧凑化(Qoder 日志真实形态)', () => {
  it('Read:file_path 剥仓库前缀', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "/home/user/repos/openclaw-manager/CONTEXT.md"}')
    expect(summarizeCliToolCall(c)).toBe('📖 阅读了 CONTEXT.md')
  })

  it('Read 带 limit:前 N 行', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "/x/services/app.py", "limit": 400}')
    expect(summarizeCliToolCall(c)).toBe('📖 阅读了 /x/services/app.py · 前 400 行')
  })

  it('Read 带 offset+limit:行范围', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "/x/app.py", "limit": 500, "offset": 1}')
    expect(summarizeCliToolCall(c)).toBe('📖 阅读了 /x/app.py · 前 500 行')
    const c2 = call('2', '调用 Read [Read]\n{"file_path": "/x/app.py", "limit": 100, "offset": 201}')
    expect(summarizeCliToolCall(c2)).toBe('📖 阅读了 /x/app.py · 第 201~301 行')
  })

  it('Grep:pattern + path', () => {
    // detail 由后端 json.dumps 生成:正则里的 \ 在 JSON 文本中是 \\(双反斜杠才是合法转义)
    const c = call('1', '调用 Grep [Grep]\n{"output_mode": "content", "path": "/home/user/repos/proj/services", "pattern": "subprocess\\\\.run"}')
    expect(summarizeCliToolCall(c)).toBe('🔍 搜索 subprocess\\.run · services')
  })

  it('Glob:pattern', () => {
    const c = call('1', '调用 Glob [Glob]\n{"path": "/home/user/repos/proj", "pattern": "tests/**/*"}')
    expect(summarizeCliToolCall(c)).toBe('🔍 查找文件 tests/**/*')
  })

  it('无 detail(旧数据)时返回 null 回退折叠', () => {
    expect(summarizeCliToolCall(call('1', '调用 Read [Read]'))).toBeNull()
  })

  it('配对成 compact 段并生成摘要', () => {
    const items = [
      call('c1', '调用 Read [Read]\n{"file_path": "/home/user/repos/proj/README.md"}'),
      result('r1', '# README 内容...'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('compact')
    if (segs[0].kind === 'compact') {
      expect(buildToolSummary(segs[0].call, segs[0].result)).toBe('📖 阅读了 README.md')
    }
  })

  it('Read result 未到达:摘要 + 省略号', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "a.py"}')
    expect(buildToolSummary(c, null)).toBe('📖 阅读了 a.py ...')
  })
})

describe('Bash/run_command 紧凑化集成', () => {
  function bashCall(cmd: string): ToolItem {
    return { id: 'b1', type: 'tool_call', content: `执行: ${cmd.slice(0, 40)} [Bash]\n${cmd}` }
  }

  it('只读 Bash 命令配对成 compact 段并生成摘要', () => {
    const items = [
      bashCall("/bin/bash -lc 'cat /home/user/repos/proj/app.py'"),
      result('r1', 'def main(): ...'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('compact')
    if (segs[0].kind === 'compact') {
      expect(buildToolSummary(segs[0].call, segs[0].result)).toBe('📖 阅读了 app.py')
    }
  })

  it('危险 Bash 命令不紧凑化,配对成 toolpair 卡片(仍可见完整结果)', () => {
    const items = [
      bashCall('rm -rf build/'),
      result('r1', 'done'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('toolpair')
  })

  it('Bash result 未到达:摘要 + 省略号', () => {
    const c = bashCall("/bin/bash -lc 'ls /home/user/repos/proj/src'")
    expect(buildToolSummary(c, null)).toBe('📂 查看目录 src ...')
  })

  it('run_command 只读命令也紧凑化', () => {
    const c = call('1', '执行命令: cat app.py [run_command]\n{"command": "cat app.py"}')
    expect(buildToolSummary(c, result('r', 'content'))).toBe('📖 阅读了 app.py')
  })
})

describe('toolFileTargetOf', () => {
  it('read_file:从结果 JSON 提取路径并拆分摘要', () => {
    const c = call('1', '读取文件 src/main.py [read_file]')
    const r = result('2', JSON.stringify({
      path: 'src/main.py', content: '...', total_lines: 356, truncated: false,
    }))
    const t = toolFileTargetOf(c, r)
    expect(t).not.toBeNull()
    expect(t!.path).toBe('src/main.py')
    expect(t!.display).toBe('src/main.py')
    expect(t!.prefix).toBe('📖 阅读了 ')
    expect(t!.suffix).toBe(' · 356 行')
  })

  it('read_file 执行中:从 intent 解析路径', () => {
    const c = call('1', '读取文件 src/app.py [read_file]\n{"file_path": "src/app.py"}')
    const t = toolFileTargetOf(c, null)
    expect(t?.path).toBe('src/app.py')
    expect(t!.prefix + t!.display + t!.suffix).toBe(buildToolSummary(c, null))
  })

  it('CLI Read:剥掉 /repos/<仓库>/ 前缀', () => {
    const c = call('1', '读取文件 /home/user/repos/proj/src/util.py [Read]\n{"file_path": "/home/user/repos/proj/src/util.py"}')
    const t = toolFileTargetOf(c, result('2', 'content'))
    expect(t?.path).toBe('src/util.py')
    expect(t!.display).toBe('src/util.py')
  })

  it('CLI Read(local 模式):Windows 绝对路径也生成可跳转目标', () => {
    // 回归:local 模式 repo_path 是宿主机临时目录,旧规则只认 /repos/ 会留下
    // 带盘符的绝对路径,前端按段名查树必然落空 → 点击文件链接静默无反应
    const fp = 'C:\\Users\\njwjx\\AppData\\Local\\Temp\\sandbox_local_5_bgvr6o\\uploaded_files\\doc.md'
    const c = call('1', `读取文件 ${fp} [Read]\n${JSON.stringify({ file_path: fp })}`)
    const t = toolFileTargetOf(c, result('2', 'content'))
    expect(t?.path).toBe('doc.md')
    expect(t!.display).toBe('doc.md')
    expect(t!.prefix + t!.display + t!.suffix).toBe(buildToolSummary(c, result('2', 'content')))
  })

  it('Bash cat 单文件:提取跳转目标', () => {
    const c = call('b1', '执行: cat src/main.py [Bash]\ncat src/main.py')
    const t = toolFileTargetOf(c, result('r', 'content'))
    expect(t?.path).toBe('src/main.py')
  })

  it('非读文件工具返回 null', () => {
    const c = call('1', '搜索代码: TODO [search_code]')
    const r = result('2', JSON.stringify({ total_matches: 3, matches: [] }))
    expect(toolFileTargetOf(c, r)).toBeNull()
  })

  it('Bash 多文件 cat 返回 null', () => {
    const c = call('b1', '执行: cat a.py b.py [Bash]\ncat a.py b.py')
    expect(toolFileTargetOf(c, result('r', 'x'))).toBeNull()
  })

  it('Bash 非只读命令返回 null', () => {
    const c = call('b1', '执行: rm a.py [Bash]\nrm a.py')
    expect(toolFileTargetOf(c, result('r', 'x'))).toBeNull()
  })
})
