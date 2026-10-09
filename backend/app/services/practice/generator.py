"""题目生成:把审查任务的真实发现(Result)改编为客观练习题

流程:
1. 取任务 Results(上限 max_findings 条,防 LLM 成本失控);
   本用户已就该 finding 出过题的直接整条跳过(去重前置到 LLM 之前,
   否则题目出完才发现 dedup_hash 撞上,钱已经花了)
2. 逐条 finding 调 LLM 生成 1~3 题:
   - system prompt 按发现内容自动匹配的主题切换:
     主题词表来自用户设置(learning_topics 表,内置 4 个 + 自定义,
     仅启用主题参与;规则先行 + LLM 批量兜底),内置主题用专有
     出题视角,自定义主题用通用模板 + 用户描述
   - 提示词强制题目必须阅读真实材料(代码或文书原文)才能作答(禁止常识题);
     工作区可用时先按 finding 的 file_path/line 定位**批量预读**材料
     (同文件多 finding 共用一次读取),片段直拼进 prompt 并改用
     「按需补读」提示词变体 → 多数 finding 0 工具轮次出题;
     未命中预读的仍走只读迷你工具循环(read_file / search_code / find_files);
     沙箱已清理且用户开启「出题前恢复工作区」时先重新 clone 恢复
3. json_repair 容错解析 + 字段校验,失败重试 1 次,仍失败丢弃该 finding;
   散文包 JSON 的输出先抽平衡数组挽救(思考类模型常见形态),不白丢整条;
   反馈重试在既有消息历史上追加反馈句,不重建工具循环(不重读文件);
   429 撞限不占重试预算,等待后原地重试
   两级质量关卡拦截不合格题(全部被拦时带质量反馈续写 1 次后丢弃):
   - 关卡 1(始终启用):退化题 — 叙述式判断题(某同学做了某判断是否
     正确)或判断题措辞泄露答案(题干含「仅凭」等)
   - 关卡 2(工作区可用时):无 code_snippet 的题,不看材料也能作答
4. 致命错误快速失败:模型 401/403(额度耗尽/Key 失效)等不可重试错误
   立即中止剩余 finding,抛 PracticeGenerateError 由 job 层展示原因
5. 知识点 get_or_create(优先 CWE 编号;job 内按 key 缓存,免每题一次 SELECT)
   + 同用户 sha256 去重
6. 落库为 draft(记录出题时实际匹配的主题与源码定位),前端预览确认后转 active

出题模型解析:task.llm_config_id > 用户级默认出题模型
(practice_settings.default_llm_config_id) > env 默认,逐级回退;
用户开启「始终用默认出题模型」(force_default_llm)时跳过任务级配置,
直接按 用户级默认 > env 默认 解析。
"""
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from json_repair import repair_json
from sqlalchemy.orm import Session

import openai
from app.config import settings
# 二进制占位文案(与 sandbox_tools 读取路径同源;判定口径见 app/file_kinds.py)
from app.file_kinds import BINARY_PLACEHOLDER
from app.llm.client import LLMClient
from app.models.practice import (
    BUILTIN_TOPIC_DEFS,
    DEFAULT_LEARNING_TOPIC,
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
    LEARNING_TOPIC_CONTRACT,
    LEARNING_TOPIC_SECURITY,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
    KnowledgePoint,
    PracticeSettings,
    Question,
    QuestionStatus,
    QuestionType,
    ensure_user_topics,
)
from app.models.task import Result, Task
from app.models.audit import ReviewItem  # 溯源:题目可回指审查项(证据驱动可信审查)
from app.models.user_llm_config import UserLLMConfig
from app.services.practice.difficulty import clamp_difficulty
from app.services.practice.explainer import (
    add_digest,
    explain_knowledge_points,
    make_source_digest,
)
from app.services.practice.parse_utils import (
    salvage_json_array as _salvage_json_array,
)
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)

# 单条 finding 最多生成题数
MAX_QUESTIONS_PER_FINDING = 3
# 解析失败重试次数
PARSE_RETRY = 1
# 出题工具循环最大轮次(每轮可含多次并行工具调用;超限后强制无工具出题)
MAX_TOOL_ROUNDS = 6
# 单次工具结果回传 LLM 的截断阈值(防上下文爆炸)
_MAX_TOOL_RESULT_CHARS = 3000
# 限流(429)原地等待重试:不占质量/解析反馈的重试预算(撞限不是模型答得差)
_RATE_LIMIT_INPLACE_WAITS = 2
_RATE_LIMIT_WAIT_SECONDS = 5.0
# 反馈续写可复用的消息历史文本量上限(超限则重建对话,避免把上下文顶爆)
_MAX_CARRY_CHARS = 60000
# token 事件合并阈值:够这个字符数或够这个时间才推一条 SSE 事件
# (逐 chunk 推会让单 job 事件数涨到上千条:写端每条抢一次全局锁,
# SSE 读端每次轮询又在全局锁内线性扫全表 → 出题线程与展示相互拖慢;
# 200ms 粒度对侧栏打字机效果无影响)
_TOKEN_BATCH_CHARS = 256
_TOKEN_BATCH_SECONDS = 0.2


class PracticeGenerateError(Exception):
    """出题过程遇到不可重试的致命错误(已生成的题目照常落库)

    异常消息为面向用户的友好原因,由 job 层捕获后置 error 终态,
    前端出题进度侧栏直接展示。
    """


class PracticeGenerateCancelled(Exception):
    """用户在出题开始前停止了 job(克隆/主题分类阶段)

    有意**不**继承 PracticeGenerateError:调用方的 `except PracticeGenerateError`
    会把终止当成失败置 error 终态。本异常只可能在没有产出题目前抛出
    (循环内停止走正常返回路径,已生成的题目先 commit 再返回),
    job 层据此置 cancelled。
    """


def _fatal_llm_reason(e: Exception) -> str | None:
    """识别出题模型的不可重试致命错误(认证/额度),返回面向用户的原因

    额度耗尽/Key 失效类错误后续所有 finding 必然同样失败,
    应立即中止(快速失败)而非逐条重试空转;其他错误(超时/
    5xx/429 等)返回 None,走原有重试与丢弃路径。
    """
    if isinstance(e, openai.AuthenticationError):
        return "API Key 无效或已失效(401),请检查出题模型配置"
    if isinstance(e, openai.PermissionDeniedError):
        text = str(e).lower()
        if "quota" in text or "free tier" in text:
            return "免费额度已用尽(403),请充值或更换出题模型"
        return "无权访问该模型(403),请检查出题模型配置"
    return None

# ============================================================
# 提示词资产:集中管理于 app/prompts/practice.py,此处仅导入
# ============================================================
from app.prompts.practice import (
    _DEGEN_FEEDBACK,
    _FINDING_TEMPLATE,
    _LEARNING_NOTE_TEMPLATE,
    _MATERIAL_SECTION_TEMPLATE,
    _NO_CODE_FEEDBACK,
    _PRACTICE_TOOL_DEFINITIONS,
    build_system_prompt,
    build_topic_classify_prompt,
)

# ============================================================
# 材料预读:按发现的源码定位一次性读好,压掉逐条发现的工具往返
# ============================================================

# 定位元信息可能的 key(场景与 agent 输出并不完全统一,按序容错匹配)
_FILE_META_KEYS = ("file_path", "filepath", "source_file", "path", "file")
_LINE_META_KEYS = ("line_range", "lines", "line", "start_line", "lineno", "location")
# 行窗口两侧扩展行数(给 LLM 必要的上下文:导入段与函数签名)
_PREFETCH_LINE_PAD = 40
# 单次读取的最大行数(超大窗口截断,防一个文件吃掉整个上下文)
_PREFETCH_MAX_LINES = 240
# 单条 finding 最多附几个材料片段
_PREFETCH_MAX_FILES_PER_FINDING = 3
# 单个片段进提示词的字符上限
_PREFETCH_MAX_CHARS_PER_FILE = 2400
# 相邻区间合并阈值(两窗口间隙小于此值就一次读完,省一次往返)
_PREFETCH_MERGE_GAP = 8
# 行号提取:兼容 "42" / "L42" / "120-150" / "120:150" / "第 120 行"
_DIGITS_RE = re.compile(r"\d+")


def _meta_first_str(meta: dict, keys: tuple[str, ...]) -> str:
    """按候选 key 取第一个非空字符串值(大小写不敏感)"""
    lowered = {str(k).lower(): v for k, v in (meta or {}).items()}
    for key in keys:
        val = lowered.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _parse_line_window(raw: str) -> tuple[int, int] | None:
    """从定位文本抽行区间;抽不到返回 None(表示整文件待 LLM 自己找)

    兼容 "42" / "L42" / "120-150" / "120:150" / "第 120 行" / "42, 距 10 行" 等写法:
    取文本里的数字串,1 个当单行,≥ 2 个取 [min, max](相等则单行)。
    """
    nums = [int(m) for m in _DIGITS_RE.findall(raw or "")]
    if not nums:
        return None
    start, end = nums[0], (nums[-1] if len(nums) > 1 else nums[0])
    if start <= 0:
        start = end
    if end < start:
        start, end = end, start
    return start, end


def _finding_material_targets(meta: dict) -> list[tuple[str, int | None, int | None]]:
    """从发现元信息抽 (文件路径, 起始行, 结束行) 预读目标

    只信 file_path 类字段;行定位缺失时给整文件开头(前 N 行),
    仍然比让 LLM 自己盲探一次便宜。
    """
    path = _meta_first_str(meta, _FILE_META_KEYS)
    if not path:
        return []
    window = _parse_line_window(_meta_first_str(meta, _LINE_META_KEYS))
    start, end = window if window else (None, None)
    return [(path, start, end)]


def _merge_read_ranges(
    windows: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """把同一文件的多条行窗口两侧加宽后合并重叠/紧邻区间,并按上限截断

    返回排序后的 (offset, max_lines) 列表;None 行定位归一为文件开头。
    """
    padded: list[tuple[int, int]] = []
    for start, end in windows:
        s = max(1, (start or 1) - _PREFETCH_LINE_PAD)
        e = (end or start or _PREFETCH_MAX_LINES) + _PREFETCH_LINE_PAD
        padded.append((s, max(s, e)))
    padded.sort()
    merged: list[list[int]] = []
    for s, e in padded:
        if merged and s - merged[-1][1] <= _PREFETCH_MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    # 截断单区间跨度(靠前的窗口优先,它就是 finding 标注的位置)
    return [
        (s, min(e, s + _PREFETCH_MAX_LINES - 1))
        for s, e in merged
    ]


def _prefetch_materials(
    task_id: str, repo_path: str, findings: list[Result],
) -> dict[str, list[dict]]:
    """按发现的源码定位批量预读材料,返回 {finding_id: [片段]}

    动机:同一文件的多个 finding 以前各自跑一遍 read_file 工具轮次
    (每条 finding 多 1~3 次 LLM 往返);而 Result.metadata 已经带了
    file_path / line 定位。这里按文件归并行窗口、去重读取一次,
    片段直接拼进 user prompt → 多数 finding 可 0 工具轮次出题。

    失败(文件不存在/二进制/沙箱异常)只记日志不抛出,该 finding
    退回原来的强制工具阅读路径。单线程执行:不对沙箱会话做跨线程假设。
    """
    if not repo_path:
        return {}
    # path → [(finding_id, start, end)]
    plan: dict[str, list[tuple[str, int | None, int | None]]] = {}
    for f in findings:
        for path, start, end in _finding_material_targets(f.metadata_ or {}):
            plan.setdefault(path, []).append((str(f.id), start, end))
    if not plan:
        return {}

    reads: dict[str, list[tuple[int, int, dict]]] = {}
    for path, targets in plan.items():
        ranges = _merge_read_ranges([(s, e) for _, s, e in targets])
        got: list[tuple[int, int, dict]] = []
        for offset, max_lines in ranges:
            try:
                res = sandbox_tools.read_file(
                    repo_path, path,
                    max_lines=max(1, min(max_lines - offset + 1, _PREFETCH_MAX_LINES)),
                    offset=offset,
                    task_id=task_id,
                )
            except Exception as e:
                logger.warning(
                    "[practice] 预读材料失败 task=%s path=%s: %s", task_id, path, e,
                )
                continue
            content = str((res or {}).get("content") or "").strip()
            # 二进制文件(docx/pdf/图片…)没有可出题的材料:占位文案与 binary
            # 标记都拦下(标记来自 sandbox_tools 的统一判定,文案是历史兼容面)
            if not content or (res or {}).get("binary") or content.startswith(BINARY_PLACEHOLDER):
                continue
            got.append(
                (int((res or {}).get("start_line") or offset),
                 int((res or {}).get("end_line") or offset), res),
            )
        reads[path] = got

    out: dict[str, list[dict]] = {}
    for path, targets in plan.items():
        available = reads.get(path) or []
        for fid, start, end in targets:
            buckets = out.setdefault(fid, [])
            for r_start, r_end, res in available:
                if len(buckets) >= _PREFETCH_MAX_FILES_PER_FINDING:
                    break
                # 行定位已知且与本次读取区间不交叠 → 不属本 finding 的材料
                if start is not None:
                    want_lo = max(1, start - _PREFETCH_LINE_PAD)
                    want_hi = (end or start) + _PREFETCH_LINE_PAD
                    if not (r_start <= want_hi and want_lo <= r_end):
                        continue
                buckets.append({
                    "path": path,
                    "lines": f"{r_start}-{r_end}",
                    "content": str(res.get("content") or "")[:_PREFETCH_MAX_CHARS_PER_FILE],
                })
    return {k: v for k, v in out.items() if v}


def _render_material_section(snippets: list[dict]) -> str:
    """预读片段渲染为提示词段(空列表返回空串,调用方据此选提示词变体)"""
    if not snippets:
        return ""
    items = [
        {"path": s["path"], "lines": s["lines"], "content": s["content"]}
        for s in snippets
    ]
    return _MATERIAL_SECTION_TEMPLATE.format(
        materials=json.dumps(items, ensure_ascii=False)
    )


# ============================================================
# 主题自动匹配:规则先行 + LLM 批量兜底(主题词表来自用户设置)
# ============================================================

# metadata 值里的 CWE 编号模式(如 "CWE-89")
_CWE_PATTERN = re.compile(r"CWE-\d+", re.IGNORECASE)


def _match_topic_by_rule(task: Task, meta: dict | None) -> str | None:
    """规则匹配单条发现的主题;判不定返回 None(交 LLM 批量分类)

    只保留两条强规则,避免误伤:
    1. 文书审核场景 → 全部 contract(场景级强信号)
    2. metadata 带 CWE 编号(键名含 cwe 或值匹配 CWE-\\d)→ security
    其余(如"知识点提炼"类发现常无 file_path,无法凭元信息区分代码/文书)
    统一交 LLM 按内容判断。
    """
    # 场景强信号:文书审核任务全部按合同文书出题
    if (task.scenario or "") == "document_review":
        return LEARNING_TOPIC_CONTRACT
    for k, v in (meta or {}).items():
        if "cwe" in str(k).lower():
            return LEARNING_TOPIC_SECURITY
        if v and _CWE_PATTERN.search(str(v)):
            return LEARNING_TOPIC_SECURITY
    return None


def _classify_topics_with_llm(
    client: LLMClient, pending: list[tuple[int, Result]], task_id: str,
    topic_defs: list[dict],
) -> dict[int, str]:
    """把规则未定的 findings 一次批量送 LLM 分类,返回 {序号: topic}

    topic_defs 为用户启用主题词表(内置 + 自定义,{key,name,description}),
    分类提示词按词表动态构建,输出必须命中词表内的 key。
    任何异常由调用方捕获降级;输出解析容错,个别条目非法只跳过该条。
    """
    items = []
    for idx, f in pending:
        items.append({
            "id": idx,
            "title": (f.title or "")[:200],
            "content": (f.content or "")[:400],
        })
    user_prompt = (
        "以下是待分类的发现列表(JSON 数组):\n"
        + json.dumps(items, ensure_ascii=False)
        + "\n请对每条判断出题主题,按系统要求的格式输出。"
    )
    messages: list[dict] = [
        {"role": "system", "content": build_topic_classify_prompt(topic_defs)},
        {"role": "user", "content": user_prompt},
    ]
    content, _ = _stream_one_round(client, messages, None)
    parsed = json.loads(repair_json(content))
    valid_keys = {d["key"] for d in topic_defs}
    result: dict[int, str] = {}
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                i = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            t = str(item.get("topic") or "").strip()
            if t in valid_keys:
                result[i] = t
    return result


def _match_finding_topics(
    task: Task, findings: list[Result], client: LLMClient,
    topic_defs: list[dict],
) -> dict:
    """逐 finding 匹配出题主题:规则先行,判不定的批量送 LLM 分类

    topic_defs 为用户启用主题词表(按 sort_order 排序)。
    返回 {finding.id: topic}。LLM 分类失败/条目缺失时降级到
    排序第一的启用主题(防该主题被停用后降级落空),保证出题不中断。
    异常静默降级,不阻断出题主流程。
    """
    fallback_topic = topic_defs[0]["key"] if topic_defs else DEFAULT_LEARNING_TOPIC
    topics: dict = {}
    pending: list[tuple[int, Result]] = []
    for idx, f in enumerate(findings):
        t = _match_topic_by_rule(task, f.metadata_ or {})
        if t:
            topics[f.id] = t
        else:
            pending.append((idx, f))

    if pending:
        try:
            classified = _classify_topics_with_llm(
                client, pending, str(task.id), topic_defs,
            )
        except Exception as e:
            logger.warning(
                "[practice] task=%s 主题 LLM 分类失败,全部降级 %s: %s",
                task.id, fallback_topic, e,
            )
            classified = {}
        for idx, f in pending:
            topics[f.id] = classified.get(idx) or fallback_topic

    # 兜底:理论上不会缺,防御性补齐
    for f in findings:
        topics.setdefault(f.id, fallback_topic)
    return topics


def _topic_stats(topic_map: dict) -> str:
    """主题分布统计(日志用),如 "security:3,contract:2" """
    counts: dict[str, int] = {}
    for t in topic_map.values():
        counts[t] = counts.get(t, 0) + 1
    return ",".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "none"


def _execute_practice_tool(task_id: str, repo_path: str, name: str, args: dict) -> str:
    """执行出题工具循环的只读工具,返回截断后的 JSON 文本"""
    try:
        if name == "read_file":
            result = sandbox_tools.read_file(
                repo_path,
                str(args.get("file_path") or ""),
                max_lines=max(1, min(int(args.get("max_lines") or 100), 150)),
                offset=max(1, int(args.get("offset") or 1)),
                task_id=task_id,
            )
        elif name == "search_code":
            result = sandbox_tools.search_code(
                repo_path,
                str(args.get("pattern") or ""),
                file_glob=args.get("file_glob"),
                max_matches=max(1, min(int(args.get("max_matches") or 30), 50)),
                output_mode=str(args.get("output_mode") or "content"),
                task_id=task_id,
            )
        elif name == "find_files":
            result = sandbox_tools.find_files(
                repo_path, str(args.get("pattern") or ""), task_id=task_id,
            )
        else:
            return f"未知工具: {name}"
        return json.dumps(result, ensure_ascii=False, default=str)[:_MAX_TOOL_RESULT_CHARS]
    except Exception as e:
        return f"工具执行失败: {e}"


def _tool_event_summary(name: str, args: dict) -> str:
    """工具事件的简短描述(侧栏展示用,如 read_file: src/x.py)"""
    if name == "read_file":
        return f"read_file: {args.get('file_path') or '?'}"
    if name == "search_code":
        return f"search_code: {args.get('pattern') or '?'}"
    if name == "find_files":
        return f"find_files: {args.get('pattern') or '?'}"
    return name


def _stream_one_round(
    client: LLMClient, messages: list[dict], tools: list[dict] | None,
    on_event: Callable[[str, dict], None] | None = None,
) -> tuple[str, list[dict]]:
    """一轮 chat_stream:累积正式回复与工具调用增量(参数跨 chunk 拼接)

    on_event(可选):content 增量实时回调 token 事件(出题进度侧栏流式展示用);
    不含 reasoning_delta(思考链噪音大)。
    """
    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    for chunk in client.chat_stream(messages, max_tokens=4096, tools=tools):
        if chunk.content_delta:
            content_parts.append(chunk.content_delta)
            if on_event:
                try:
                    on_event("token", {"delta": chunk.content_delta})
                except Exception:
                    pass  # 事件回调失败不影响出题主流程
        for d in chunk.tool_call_deltas or []:
            slot = tool_calls_acc.setdefault(
                d.index, {"id": "", "name": "", "arguments_str": ""},
            )
            if d.id and not slot["id"]:
                slot["id"] = d.id
            if d.name and not slot["name"]:
                slot["name"] = d.name
            if d.arguments_fragment:
                slot["arguments_str"] += d.arguments_fragment
    tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return "".join(content_parts), tool_calls


def _new_llm_turn(system_prompt: str, finding_text: str) -> list[dict]:
    """开一轮出题对话的消息底座(供 _call_llm 原地续写)"""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": finding_text},
    ]


def _turn_chars(messages: list[dict]) -> int:
    """消息历史文本量(粗估续写是否会把上下文顶爆)"""
    total = 0
    for m in messages or []:
        total += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += len(str((tc.get("function") or {}).get("arguments") or ""))
    return total


def _call_llm(
    client: LLMClient, system_prompt: str, finding_text: str,
    task_id: str, repo_path: str,
    on_event: Callable[[str, dict], None] | None = None,
    messages: list[dict] | None = None,
) -> str:
    """出题 LLM 调用:工作区可用 → 有界工具循环;否则单次直出

    messages(可选):**续写用的消息历史**(本函数会原地追加 assistant/tool 消息)。
    质量/解析反馈重试时传上一轮的 messages 并追加反馈句:材料已在上下文里,
    模型不必重读文件。以前重试是从原始 prompt 重建整个工具循环,
    同一批文件重读一遍,单条 finding 成本近乎翻倍。
    传了 messages 时 system_prompt/finding_text 不再参与重建(调用方已把
    反馈作为 user 消息追加),保留这两个形参只为兼容旧签名与测试置假。
    """
    tools = _PRACTICE_TOOL_DEFINITIONS if repo_path else None
    if messages is None:
        messages = _new_llm_turn(system_prompt, finding_text)
    for _ in range(MAX_TOOL_ROUNDS):
        content, tool_calls = _stream_one_round(client, messages, tools, on_event)
        if not tool_calls:
            return content
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments_str"]},
            }
            for i, tc in enumerate(tool_calls)
        ]
        messages.append(assistant_msg)
        for i, tc in enumerate(tool_calls):
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}
            if on_event:
                try:
                    on_event("tool", {
                        "name": tc["name"],
                        "summary": _tool_event_summary(tc["name"], args),
                    })
                except Exception:
                    pass
            result_str = _execute_practice_tool(task_id, repo_path, tc["name"], args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{i}",
                "content": result_str,
            })
    # 超工具轮数:去掉工具强制收口出题
    content, _ = _stream_one_round(client, messages, None, on_event)
    return content


# ============================================================
# 工作区保障(沙箱已清理时按用户设置重新 clone)
# ============================================================


def _load_git_tokens(db: Session, user_id) -> dict[str, str]:
    """加载用户 git provider 的 access_token(与 orchestrator._load_git_tokens 同逻辑)"""
    if user_id is None:
        return {}
    try:
        from app.models.user_git_binding import UserGitBinding
        from app.security import decrypt_secret

        bindings = (
            db.query(UserGitBinding)
            .filter(
                UserGitBinding.user_id == user_id,
                UserGitBinding.access_token != "",
            )
            .all()
        )
        tokens: dict[str, str] = {}
        for b in bindings:
            try:
                tokens[b.provider] = decrypt_secret(b.access_token)
            except Exception as e:
                logger.warning("[practice] 解密 %s token 失败: %s", b.provider, e)
        return tokens
    except Exception as e:
        logger.warning("[practice] 加载 git token 失败: %s", e)
        return {}


def _ensure_workspace(
    db: Session, task: Task, settings_row: PracticeSettings | None,
    event_callback: Callable[[str, dict], None] | None = None,
    git_tokens: dict[str, str] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict | None:
    """保障出题用的工作区,返回 workspace info(含 repo_path;不可用为 None)

    - 沙箱 session 存活 → 直接复用
    - 已清理 + 用户开启 restore_workspace_for_practice + 任务有 repo_url
      → 重新 clone 恢复(成功后标记 completed 纳入 1 小时 TTL 清理序列)
    - 恢复失败/条件不满足 → 静默降级(出题走无工具路径)

    event_callback 非空时推 restore 事件(start/progress/done/failed),
    供出题进度侧栏展示克隆进度;回调异常不影响恢复主流程。

    git_tokens 可预先传入(主线程读库拿到):恢复可能跑在后台线程,
    而 SQLAlchemy Session 非线程安全 —— 传了就在线程内再查库。

    cancel_check 非空时传给克隆轮询做取消检查点(用户停止出题时大仓库克隆
    可能还要跑几分钟,不在这里拦的话停止按钮在克隆阶段完全不生效);
    命中时抛 PracticeGenerateCancelled 而不是降级为无工具出题 ——
    用户要的是"停下",不是"换种方式继续跑"。
    """

    def _emit(phase: str, **extra) -> None:
        if event_callback is None:
            return
        try:
            event_callback("restore", {"phase": phase, **extra})
        except Exception:
            logger.warning("[practice] restore 事件回调异常(忽略)", exc_info=True)

    task_id_str = str(task.id)
    info = sandbox_tools.get_workspace_info(task_id_str)
    if info and info.get("repo_path"):
        return info
    params = task.params or {}
    repo_url = params.get("repo_url")
    if not repo_url or settings_row is None or not settings_row.restore_workspace_for_practice:
        return info
    logger.info("[task=%s] 出题前沙箱已清理,重新 clone 恢复工作区", task.id)
    _emit("start")
    # cancel_check 仅非空时下传:存量克隆替身不接这个参数
    cancel_kw: dict[str, Any] = (
        {} if cancel_check is None else {"cancel_check": cancel_check}
    )
    try:
        sandbox_tools.clone_repo_with_fallback(
            repo_url,
            branch=params.get("branch"),
            task_id=task_id_str,
            git_tokens=git_tokens if git_tokens is not None
            else _load_git_tokens(db, task.user_id),
            progress_callback=lambda percent, message: _emit(
                "progress", percent=percent, message=message,
            ),
            **cancel_kw,
        )
        # 恢复的 session 属于已完成任务:纳入 TTL 清理序列,避免常驻泄漏
        sandbox_tools.mark_task_completed(task_id_str)
        _emit("done")
        return sandbox_tools.get_workspace_info(task_id_str)
    except sandbox_tools.CloneCancelledError as e:
        # 用户已停止出题:先推 restore/failed 让侧栏收起横幅,再把取消冒到 job 层。
        # 必须排在 except Exception 之前:否则会被当成"克隆失败降级",
        # 出题拿着空工作区继续跑,LLM 成本照付
        _emit("failed", message="已按用户请求停止(克隆已中断)")
        raise PracticeGenerateCancelled(str(e) or "出题已停止") from e
    except Exception as e:
        logger.warning("[task=%s] 出题前恢复工作区失败(降级为无工具出题): %s", task.id, e)
        _emit("failed", message=str(e)[:200])
        return sandbox_tools.get_workspace_info(task_id_str)


# ============================================================
# 原有管线:模型解析 / 校验 / 去重 / 落库
# ============================================================


def resolve_llm_client(db: Session, task: Task) -> LLMClient:
    """解析出题模型:task.llm_config_id > 用户级默认出题模型 > env 默认

    任务级与用户级配置都存于 UserLLMConfig.llm_configs(一次查询,
    按优先级逐个匹配);任一级配置缺失/失效均回退下一级,全部失败回退 env 默认。
    用户开启「始终用默认出题模型」(practice_settings.force_default_llm)时
    跳过任务级配置,直接按 用户级默认 > env 默认 解析。
    手动出题与任务完成自动出题共用本解析。
    """
    config_ids: list[str] = []
    pref = None
    if task.user_id is not None:
        pref = db.query(PracticeSettings).filter(
            PracticeSettings.user_id == task.user_id
        ).first()
    # 「始终用默认出题模型」开启时忽略任务自带配置(用户级默认 > env 默认)
    force_default = pref is not None and pref.force_default_llm is True
    if not force_default and task.llm_config_id:
        config_ids.append(task.llm_config_id)
    if pref is not None and pref.default_llm_config_id:
        config_ids.append(pref.default_llm_config_id)
    if config_ids:
        try:
            cfg_row = db.query(UserLLMConfig).filter(
                UserLLMConfig.user_id == task.user_id
            ).first()
            configs = {
                c.get("id"): c for c in (cfg_row.llm_configs or [])
            } if cfg_row else {}
            for cid in config_ids:
                cfg = configs.get(cid)
                if cfg:
                    return LLMClient.from_config_dict(cfg)
            logger.warning(
                "[practice] 未找到出题模型配置 ids=%s,回退 env 默认", config_ids
            )
        except Exception as e:
            logger.warning("[practice] 加载出题模型配置失败,回退 env 默认: %s", e)
    return LLMClient()


def resolve_explain_client(
    db: Session, user_id, settings_row: PracticeSettings | None,
    fallback: LLMClient,
) -> LLMClient:
    """讲解模型解析:practice_settings.explain_llm_config_id > 出题模型(fallback)

    讲解是轻任务:用思考类模型出题要 ~160s/条,写一段讲解用不着那个代价,
    所以允许单独挂个快/便宜的模型。配置缺失或失效静默回退出题模型。
    """
    cid = getattr(settings_row, "explain_llm_config_id", None)
    if not cid or user_id is None:
        return fallback
    client = _client_from_config_id(db, user_id, cid)
    if client is None:
        logger.warning("[explain] 未找到讲解模型配置 id=%s,回退出题模型", cid)
        return fallback
    return client


def resolve_explain_client_for_user(
    db: Session, user_id, settings_row: PracticeSettings | None,
) -> LLMClient:
    """看板按需生成讲解时的模型:讲解专用 > 默认出题模型 > env 默认

    这条路径没有 task(与任务无关,只围绕知识点),所以不走任务级配置。
    """
    cid = (
        getattr(settings_row, "explain_llm_config_id", None)
        or getattr(settings_row, "default_llm_config_id", None)
    )
    if cid and user_id is not None:
        client = _client_from_config_id(db, user_id, cid)
        if client is not None:
            return client
    return LLMClient()


def _client_from_config_id(
    db: Session, user_id, config_id: str,
) -> LLMClient | None:
    """按 UserLLMConfig 里的配置 id 构造客户端;找不到/构造失败返回 None"""
    try:
        cfg_row = db.query(UserLLMConfig).filter(
            UserLLMConfig.user_id == user_id
        ).first()
        for c in ((cfg_row.llm_configs or []) if cfg_row else []):
            if c.get("id") == config_id:
                return LLMClient.from_config_dict(c)
    except Exception as e:
        logger.warning("[practice] 加载模型配置 id=%s 失败: %s", config_id, e)
    return None


def user_custom_topic_defs(db: Session, user_id) -> list[dict]:
    """用户启用中的自定义学习主题({key,name,description})

    出题与知识点讲解共用同一份词表,保证自定义主题的口径一致。
    """
    builtin = {b["key"] for b in BUILTIN_TOPIC_DEFS}
    return [
        {"key": t.key, "name": t.name, "description": t.description}
        for t in ensure_user_topics(db, user_id)
        if t.enabled and t.key not in builtin
    ]


def resolve_generate_model_info(db: Session, task: Task) -> dict[str, str]:
    """解析本次出题将使用的模型与来源(任务详情页展示用)

    与 resolve_llm_client 同一优先级(含「始终用默认出题模型」开关),
    返回 {model: 模型名, source: task=任务配置 / default=练习默认 / env=环境默认}。
    """
    client = resolve_llm_client(db, task)
    model = getattr(client, "model", None) or "?"
    source = "env"
    if task.user_id is not None:
        pref = db.query(PracticeSettings).filter(
            PracticeSettings.user_id == task.user_id
        ).first()
        if pref is not None and pref.force_default_llm is True:
            if pref.default_llm_config_id:
                source = "default"
        elif task.llm_config_id:
            source = "task"
        elif pref is not None and pref.default_llm_config_id:
            source = "default"
    elif task.llm_config_id:
        source = "task"
    return {"model": model, "source": source}


def _apply_thinking_mode(client: LLMClient, settings_row: PracticeSettings | None) -> None:
    """应用用户级出题思考模式覆盖(手动/自动出题共用)

    follow(默认)保持出题模型配置自身的思考开关不动;on/off 强制开/关:
    - 思考模式出题更慢但可能质量更高;部分模型思考模式下工具调用
      会写成文本而非结构化通道,导致出题工具循环失效,此时可强制关
    - 仅支持思考模式的模型(catalog thinking=only)强制关无效:
      build_thinking_extras 对该类模型始终强附思考参数,这里记日志后跳过
    """
    mode = getattr(settings_row, "thinking_mode_for_practice", None)
    if mode not in (THINKING_MODE_OFF, THINKING_MODE_ON):
        return  # follow / 未知值(如测试 mock)→ 保持模型配置原样
    meta = getattr(client, "model_meta", None) or {}
    if mode == THINKING_MODE_OFF and meta.get("thinking") == "only":
        logger.info(
            "[practice] 模型 %s 仅支持思考模式,忽略强制关闭设置",
            getattr(client, "model", "?"),
        )
        return
    client.enable_thinking = (mode == THINKING_MODE_ON)


def compute_dedup_hash(stem: str, code_snippet: str | None) -> str:
    return hashlib.sha256(
        f"{stem.strip()}\n{(code_snippet or '').strip()}".encode("utf-8")
    ).hexdigest()


# 文件扩展名 → 语言短名(LLM 未给 languages 时从 source_file 推断用)
_EXT_LANGUAGES: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".cc": "cpp", ".cs": "csharp", ".php": "php", ".rb": "ruby",
    ".sql": "sql", ".sh": "shell", ".html": "html", ".vue": "vue",
}

# 语言标签规范化上限(防 LLM 乱输出)
_MAX_LANGUAGES = 5
_MAX_LANGUAGE_LEN = 24


def _normalize_languages(raw: Any, source_file: str | None) -> list[str]:
    """规范化 LLM 输出的语言标签:小写/去重/截断;未给时从 source_file 扩展名推断"""
    langs: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            s = str(item or "").strip().lower()
            if s and s not in langs:
                langs.append(s[:_MAX_LANGUAGE_LEN])
    if not langs and source_file:
        ext = source_file[source_file.rfind("."):].lower() if "." in source_file else ""
        lang = _EXT_LANGUAGES.get(ext)
        if lang:
            langs.append(lang)
    return langs[:_MAX_LANGUAGES]


def _merge_kp_languages(kp: KnowledgePoint, languages: list[str] | None) -> None:
    """知识点语言标签并集累积(保持原顺序追加新标签)"""
    merged = list(kp.languages or [])
    added = [l for l in (languages or []) if l not in merged]
    if added:
        kp.languages = merged + added


def _get_or_create_knowledge_point(
    db: Session, user_id, key: str, name: str, languages: list[str] | None = None,
    learning_topic: str = DEFAULT_LEARNING_TOPIC,
    cache: dict[str, "KnowledgePoint"] | None = None,
) -> KnowledgePoint:
    key = (key or "").strip() or "general"
    if cache is not None and key in cache:
        # 本轮已取过该知识点:只做语言标签并集,省掉一次 SELECT
        # (一次 10 finding 的 job 会反复命中 CWE-89 这类高频知识点)
        kp = cache[key]
        _merge_kp_languages(kp, languages)
        return kp
    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == user_id,
        KnowledgePoint.key == key,
    ).first()
    if kp:
        # 已有知识点:语言标签并集累积(保持原顺序追加新语言);
        # learning_topic first-wins(首个出题主题归属保持稳定)
        _merge_kp_languages(kp, languages)
        if cache is not None:
            cache[key] = kp
        return kp
    kp = KnowledgePoint(
        user_id=user_id,
        key=key,
        name=(name or "").strip() or key,
        category="cwe" if key.upper().startswith("CWE-") else "general",
        languages=list(languages or []),
        learning_topic=learning_topic,
    )
    db.add(kp)
    db.flush()
    if cache is not None:
        cache[key] = kp
    return kp


def _normalize_raw_question(raw: Any, finding_meta: dict) -> dict | None:
    """校验并规范化 LLM 输出的单题结构,非法返回 None"""
    if not isinstance(raw, dict):
        return None

    qtype = str(raw.get("qtype") or "").strip()
    if qtype not in ("single_choice", "true_false"):
        return None

    stem = str(raw.get("stem") or "").strip()
    if not stem:
        return None

    options = raw.get("options")
    if qtype == "true_false":
        options = ["正确", "错误"]
    else:
        if not isinstance(options, list):
            return None
        options = [str(o).strip() for o in options if str(o).strip()]
        if len(options) < 2 or len(options) > 8:
            return None

    try:
        answer_idx = int(raw.get("answer_idx"))
    except (TypeError, ValueError):
        return None
    if not (0 <= answer_idx < len(options)):
        return None

    # 单选题全同选项无效
    if qtype == "single_choice" and len(set(options)) < 2:
        return None

    difficulty = raw.get("difficulty", 3)
    try:
        difficulty = clamp_difficulty(float(difficulty))
    except (TypeError, ValueError):
        difficulty = 3.0

    # knowledge_key 优先用 finding 元信息里的 CWE(比 LLM 输出可靠)
    cwe = str(finding_meta.get("cwe") or "").strip().upper()
    if cwe and not cwe.startswith("CWE-") and cwe.isdigit():
        cwe = f"CWE-{cwe}"
    knowledge_key = cwe or str(raw.get("knowledge_key") or "").strip() or "general"

    code_snippet = raw.get("code_snippet")
    code_snippet = str(code_snippet).strip() if code_snippet else None

    # 源码定位(工作区可用时 LLM 应给出;老输出无此字段时为 None)
    source_file = str(raw.get("source_file") or "").strip()[:512] or None
    source_lines = str(raw.get("source_lines") or "").strip()[:32] or None

    # 出题形式:repo=真实代码题,synthetic=改编题;白名单外回退 repo
    origin = str(raw.get("origin") or "").strip()
    if origin not in ("repo", "synthetic"):
        origin = "repo"

    return {
        "qtype": qtype,
        "stem": stem,
        "code_snippet": code_snippet,
        "options": options,
        "answer_idx": answer_idx,
        "explanation": str(raw.get("explanation") or "").strip(),
        "difficulty": difficulty,
        "knowledge_key": knowledge_key,
        "knowledge_name": str(raw.get("knowledge_name") or "").strip(),
        "source_file": source_file,
        "source_lines": source_lines,
        "origin": origin,
        "languages": _normalize_languages(raw.get("languages"), source_file),
    }


def _parse_llm_questions(content: str, finding_meta: dict, finding_id=None) -> list[dict]:
    """json_repair 容错解析 LLM 输出 → 规范题目列表

    解析/校验的每个丢弃分支都落日志(出题专用日志文件),
    便于排查“一道题也没生成”是模型输出问题还是校验过严。

    输出为散文包裹的数组时(非 JSON 根)先走 _salvage_json_array 挽救,
    挽不回才丢弃。
    """
    text = (content or "").strip()
    if not text:
        logger.warning("[practice] finding=%s LLM 输出为空,无法出题", finding_id)
        return []
    try:
        result = repair_json(text, return_objects=True)
    except Exception as e:
        logger.warning(
            "[practice] finding=%s json_repair 解析失败: %s; 输出样例: %r",
            finding_id, e, text[:300],
        )
        result = None
    if isinstance(result, str) or result is None:
        # 散文 + JSON 数组(或非 JSON 文本):抽第一个平衡数组再试一次
        salvaged = _salvage_json_array(text)
        retry = None
        if salvaged:
            try:
                retry = repair_json(salvaged, return_objects=True)
            except Exception:
                retry = None
        if isinstance(retry, (list, dict)):
            logger.info(
                "[practice] finding=%s 输出非纯 JSON,已从散文中挽救出数组(长度 %d)",
                finding_id, len(salvaged or ""),
            )
            result = retry
        else:
            result = []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        logger.warning(
            "[practice] finding=%s LLM 输出解析结果非数组(%s),样例: %r",
            finding_id, type(result).__name__, text[:300],
        )
        return []
    questions = []
    invalid: list[Any] = []
    for raw in result:
        q = _normalize_raw_question(raw, finding_meta)
        if q:
            questions.append(q)
        else:
            invalid.append(raw)
    if invalid:
        try:
            sample = json.dumps(invalid[0], ensure_ascii=False, default=str)[:300]
        except Exception:
            sample = str(invalid[0])[:300]
        logger.warning(
            "[practice] finding=%s 结构校验丢弃 %d/%d 题,首个无效元素样例: %s",
            finding_id, len(invalid), len(result), sample,
        )
    if not questions and not invalid:
        # 模型主动返回空数组(提示词约定:判定误报/不适合出题时返回 [])
        logger.info(
            "[practice] finding=%s LLM 返回空数组(可能判定为误报或不适合出题)",
            finding_id,
        )
    return questions[:MAX_QUESTIONS_PER_FINDING]


def _is_practice_worthy(finding: Result) -> bool:
    """finding 是否被 agent2 标记为有学习价值(metadata.practice_worthy)"""
    meta = finding.metadata_
    return isinstance(meta, dict) and meta.get("practice_worthy") is True


def _resolve_source_review_item_id(db: Session, metadata: Any) -> UUID | None:
    """从知识点 Result.metadata_.source_review_item_id 解析出真实存在的审查项 id。

    证据驱动可信审查的出题溯源:题目源自哪条真实审查项。无效/悬空(审查项已不在)
    返回 None,避免因外键约束破坏整批入库。
    """
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("source_review_item_id")
    if not raw:
        return None
    try:
        cand = UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        exists = db.query(ReviewItem.id).filter(ReviewItem.id == cand).first()
    except Exception:
        return None
    return cand if exists else None


# ============================================================
# 质量关卡 1:退化题检测(叙述式判断题 / 答案泄露措辞)
# ============================================================

# 虚构人物叙述模式:某(位/个)同学/工程师/...、小明/小王等
_NARRATOR_PATTERN = re.compile(
    r"某(?:位|个)?(?:同学|工程师|开发(?:者|人员)?|程序员|测试(?:人员|工程师)?|"
    r"用户|新人|审计(?:员|人员)?|分析(?:师|人员)?)|小[明华红强伟芳]"
)
# 叙述式判断题的题干问法(与人物模式同时出现即为退化)
_NARRATIVE_JUDGE_PATTERN = re.compile(
    r"是否正确|正确吗|对不对|对吗|判断是否|判断正确"
)
# 判断题措辞即答案的泄露词(「仅凭 X 就 Y」的叙述必然是反例)
_ANSWER_LEAK_WORDS = ("仅凭", "就想当然", "便断定", "就直接认定", "就认定")


def _is_degenerate_question(q: dict) -> bool:
    """检测退化题:不看材料也能答对、或题型套路泄露答案的题

    两类模式:
    1. 叙述式判断题:「某同学做了某判断,该判断是否正确」— 出题惯性里
       此类叙述必然是反例,看到「某同学 + 仅凭」即可答"错误",零知识送分
    2. 判断题措辞泄露:题干含「仅凭」等泄露词时,答案恒为"错误"

    纯规则检测(零 LLM 成本),误伤可控:正常专业题不会引入虚构人物
    叙述,也不会用「仅凭」开头描述材料事实。
    """
    stem = q.get("stem") or ""
    if not stem:
        return False
    if _NARRATOR_PATTERN.search(stem) and _NARRATIVE_JUDGE_PATTERN.search(stem):
        return True
    if q.get("qtype") == "true_false":
        return any(w in stem for w in _ANSWER_LEAK_WORDS)
    return False


def _select_findings(
    db: Session, task: Task, max_findings: int,
) -> list[Result]:
    """选取出题素材:practice_worthy 标记的优先,不足再补未标记的

    标记由 agent2 在 done=true 整理 results 时写入 metadata
    (practice_worthy=true + learning_note=考察点说明)。
    无标记(单 agent 模式 / 老任务 / agent2 未标)时,
    行为与按 created_at 顺序取前 N 条完全一致,向后兼容。
    两组内部均保持 created_at 顺序(agent2 的标记顺序即结果顺序)。
    """
    all_findings = (
        db.query(Result)
        .filter(Result.task_id == task.id)
        .order_by(Result.created_at)
        .all()
    )
    marked = [f for f in all_findings if _is_practice_worthy(f)]
    if not marked:
        return all_findings[:max_findings]
    rest = [f for f in all_findings if not _is_practice_worthy(f)]
    return (marked + rest)[:max_findings]


class _TokenBatcher:
    """把逐 chunk 的 token 增量合并成块再回调(只合并 token,其余事件透传)

    顺序保证:非 token 事件(finding/tool)先 flush 累积文本再透传,
    所以侧栏不会出现「下一条 finding 已开始但上一条的尾巴才到」。
    调用方必须在一条 finding 结束后 close()(否则尾部文本会延后一个 finding 才现形)。
    """

    def __init__(
        self, sink: Callable[[str, dict], None] | None,
        *, max_chars: int = _TOKEN_BATCH_CHARS,
        max_seconds: float = _TOKEN_BATCH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._max_chars = max(1, max_chars)
        self._max_seconds = max_seconds
        self._clock = clock
        self._buf: list[str] = []
        self._chars = 0
        self._since = clock()

    def _flush(self) -> None:
        if not self._buf or self._sink is None:
            self._buf.clear()
            self._chars = 0
            return
        try:
            self._sink("token", {"delta": "".join(self._buf)})
        except Exception:
            pass  # 事件回调失败不影响出题主流程
        self._buf.clear()
        self._chars = 0
        self._since = self._clock()

    def emit(self, etype: str, data: dict) -> None:
        if self._sink is None:
            return
        if etype != "token":
            self._flush()
            try:
                self._sink(etype, data)
            except Exception:
                pass
            return
        delta = str(data.get("delta") or "")
        # 先把够块的累积内容推出去(按已有内容判定阈值),再收新片段:
        # 已存了 ≥0.2s 的文本不应等下一条才现形
        if self._buf and (
            self._chars >= self._max_chars
            or self._clock() - self._since >= self._max_seconds
        ):
            self._flush()
        self._buf.append(delta)
        self._chars += len(delta)

    def close(self) -> None:
        """收口:把末尾不足阈值的文本一次推出"""
        self._flush()


def _build_finding_prompt(
    finding: Result, meta: dict, material_text: str = "",
) -> str:
    """拼单条发现的 user prompt:发现正文 + 预读材料段 + 学习点提示"""
    prompt = _FINDING_TEMPLATE.format(
        title=finding.title,
        content=(finding.content or "")[:4000],
        metadata=meta,
    )
    # 预读到的真实材料接在发现后面(命中时提示词走「按需补读」变体)
    if material_text:
        prompt += material_text
    # agent2 标记的学习点:注入考察方向,引导出题聚焦值得学的点
    learning_note = meta.get("learning_note") if isinstance(meta, dict) else None
    if _is_practice_worthy(finding) and learning_note:
        prompt += _LEARNING_NOTE_TEMPLATE.format(note=str(learning_note)[:500])
    return prompt


def _generate_for_finding(
    client: LLMClient, system_prompt: str, prompt: str, finding: Result,
    meta: dict, task_id_str: str, repo_path: str,
    event_callback: Callable[[str, dict], None] | None = None,
) -> tuple[list[dict], str, str]:
    """单条发现的出题:LLM 往返 + 解析 + 两道质量关卡 + 至多 1 次重试

    返回 (合格题目 dict 列表, 最后一次 LLM 原文, 致命错误原因);
    致命原因非空时调用方应中止剩余 finding(已生成题目照常保留)。

    两类重试(共 1 次预算,由 PARSE_RETRY 控制):
    - 质量关卡全拦 → 带对应反馈句在既有消息历史上续写,不重建工具循环
      (材料已在上下文里,重建会把同一批文件重读一遍,单条成本近乎翻倍)
    - LLM 异常/输出不可解析 → 不带反馈原地重试(等价于旧版 for 循环的
      自然重试;思考类模型 160s 超时并不罕见,首败即丢整条 finding 太浪费)
    - 429 不占上述预算(限流不是模型答得差),单独原地等待重试

    只跑 LLM 与本地校验,不碰数据库(供后续并发化直接当工作单元用)。
    """
    messages = _new_llm_turn(system_prompt, prompt)
    call_text = prompt  # 本轮新增的 user 文本(供日志与测试可观测)
    content = ""
    questions: list[dict] = []
    fatal_reason = ""
    rate_waits = 0
    attempt = 0
    # token 增量合并后再推(见 _TokenBatcher);tool/finding 事件原序透传
    batcher = _TokenBatcher(event_callback)
    while attempt <= PARSE_RETRY:
        try:
            content = _call_llm(
                client, system_prompt, call_text, task_id_str, repo_path,
                on_event=batcher.emit, messages=messages,
            )
        except openai.RateLimitError as e:
            if rate_waits < _RATE_LIMIT_INPLACE_WAITS:
                rate_waits += 1
                wait = _RATE_LIMIT_WAIT_SECONDS * rate_waits
                logger.info(
                    "[practice] finding=%s 命中限流,%.0fs 后原地重试(%d/%d): %s",
                    finding.id, wait, rate_waits, _RATE_LIMIT_INPLACE_WAITS, e,
                )
                time.sleep(wait)
                continue
            logger.warning(
                "[practice] finding=%s 限流重试 %d 次仍失败,丢弃该条: %s",
                finding.id, rate_waits, e,
            )
            content = ""
        except Exception as e:
            fatal = _fatal_llm_reason(e)
            if fatal is not None:
                # 额度/认证类错误:后续 finding 必然同样失败,立即中止
                # 避免空转(原因经 PracticeGenerateError 推给前端展示)
                logger.error("[practice] 出题模型致命错误,中止剩余 finding: %s", e)
                fatal_reason = (
                    f"出题模型 {getattr(client, 'model', '?')} {fatal}"
                )
                break
            logger.warning(
                "[practice] finding=%s LLM 调用失败(第 %d 次): %s",
                finding.id, attempt + 1, e,
            )
            content = ""

        questions = _parse_llm_questions(content, meta, finding_id=finding.id)
        # 质量关卡 1(始终启用):退化题拦截(叙述式判断题/答案泄露措辞)
        degen_dropped = 0
        if questions:
            sane = [q for q in questions if not _is_degenerate_question(q)]
            degen_dropped = len(questions) - len(sane)
            if degen_dropped:
                logger.info(
                    "[practice] finding=%s 质量关卡: %d/%d 题为退化题"
                    "(叙述式判断/答案泄露)被丢弃",
                    finding.id, degen_dropped, len(questions),
                )
            questions = sane
        # 质量关卡 2:工作区可用时无 code_snippet 的题不合格(常识题拦截)
        no_snippet_dropped = 0
        if repo_path and questions:
            qualified = [q for q in questions if q["code_snippet"]]
            no_snippet_dropped = len(questions) - len(qualified)
            if no_snippet_dropped:
                logger.info(
                    "[practice] finding=%s 质量关卡: %d/%d 题缺 code_snippet 被丢弃",
                    finding.id, no_snippet_dropped, len(questions),
                )
            questions = qualified
        if questions or fatal_reason:
            break
        # 全部被关卡拦截:带对应质量反馈重试一次(退化题反馈优先)
        if attempt < PARSE_RETRY and (degen_dropped or no_snippet_dropped):
            feedback = _DEGEN_FEEDBACK if degen_dropped else _NO_CODE_FEEDBACK
            logger.info(
                "[practice] finding=%s 全部题目被质量关卡拦截,带反馈续写(退化 %d/缺材料 %d)",
                finding.id, degen_dropped, no_snippet_dropped,
            )
            if _turn_chars(messages) <= _MAX_CARRY_CHARS:
                # 原地续写:只追加反馈句,上一轮回答与已读到的工具结果都在历史里
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": feedback},
                ]
                call_text = prompt + feedback  # 本轮意图的完整描述(供可观测)
            else:
                # 历史太长:重建对话(退回旧行为,带反馈的完整 prompt)
                messages = _new_llm_turn(system_prompt, prompt + feedback)
                call_text = prompt + feedback
            attempt += 1
            continue
        # LLM 异常/输出不可解析(未触发任何关卡):原地重试一次(不带反馈),
        # 与模块 docstring「失败重试 1 次」口径一致(旧版 for 循环的自然重试行为)
        if attempt < PARSE_RETRY:
            logger.info(
                "[practice] finding=%s 无有效输出(LLM 失败或解析空),原地重试(第 %d/%d 次)",
                finding.id, attempt + 2, PARSE_RETRY + 1,
            )
            attempt += 1
            continue
        break
    batcher.close()
    return questions, content, fatal_reason


# ============================================================
# 可选并发:逐条 finding 并行跑 LLM,落库仍在主线程
# ============================================================


@dataclass
class _FindingWork:
    """一条 finding 的出题工作成果(LLM 部分,不含任何 DB 操作)"""

    finding: Result
    meta: dict
    topic: str
    snippets: list[dict]
    questions: list[dict] = field(default_factory=list)
    content: str = ""
    fatal: str = ""


# 厂商级并发闸门(按 provider+model 归组,跳 job 共享)
_provider_gates: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_provider_gates_lock = threading.Lock()


@contextmanager
def _provider_gate(client: LLMClient):
    """限制同一厂商同时进行中的出题请求数

    厂商侧「组内并发/RPM」是有配额的:两个出题 job 各自开 4 并行会互相扫配额,
    闸门让全进程对该厂商的在途请求不超 PRACTICE_PROVIDER_MAX_CONCURRENCY。
    并发度=1 时本质上只是个透传锁(不改变行为)。
    """
    key = (
        f"{getattr(client, 'provider_id', '')}"
        f"|{getattr(client, 'model', '')}"
        f"|{getattr(client, 'base_url_override', '') or ''}"
    )
    limit = max(1, settings.PRACTICE_PROVIDER_MAX_CONCURRENCY)
    with _provider_gates_lock:
        entry = _provider_gates.get(key)
        if entry is None or entry[0] != limit:
            entry = (limit, threading.BoundedSemaphore(limit))
            _provider_gates[key] = entry
    sem = entry[1]
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _workspace_restore_worth_bg(
    task: Task, settings_row: PracticeSettings | None, probe: dict | None,
) -> bool:
    """是否可能需要走 clone(决定要不要开后台恢复线程)

    仅作为“值不值得起线程”的提示判定,与 _ensure_workspace 内的实际闸门条件一致;
    误判为 False 也只是回到原来的串行恢复路径,不会少恢复。
    """
    if (probe or {}).get("repo_path"):
        return False  # 工作区存活,无需恢复
    if settings_row is None or not settings_row.restore_workspace_for_practice:
        return False
    params = task.params or {}
    return bool(params.get("repo_url"))


def _effective_concurrency(settings_row: PracticeSettings | None, pending_count: int) -> int:
    """本次 job 的实际并发度 = min(用户设置, 系统上限, 待处理条数)

    用户设置默认 1(串行):部分厂商只允许 1 并发,并行只会换来 429 与退避等待。
    运维可用 PRACTICE_GENERATE_CONCURRENCY=1 强制全局串行。
    """
    want = int(getattr(settings_row, "generate_concurrency", 1) or 1)
    cap = max(1, min(want, settings.PRACTICE_GENERATE_CONCURRENCY))
    return max(1, min(cap, max(1, pending_count)))


def generate_questions_for_task(
    db: Session,
    task: Task,
    user_id,
    max_findings: int = 10,
    client: LLMClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    event_callback: Callable[[str, dict], None] | None = None,
    force_regenerate: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[Question], int]:
    """为任务的 Results 生成 draft 题目

    返回 (新建题目列表, 被跳过的 finding 数)。
    progress_callback(done, total):每处理完一条 finding 回调(异步生成进度展示用)。
    event_callback(type, data):流式事件回调(finding/token/tool,
    出题进度侧栏 SSE 展示用),异常不影响出题主流程。
    force_regenerate:False 时,本用户已就该 finding 出过题的**整条跳过**
      (去重原本发生在 LLM 之后:题目出完才发现 dedup_hash 撞上,钱已花);
      任务详情页「重新出题」显式传 True 才允许重出。
    should_stop:可选回调() -> bool,用户已点「停止出题」时返回 True。命中时
      在检查点退出:题目循环内停止走正常返回路径(**已生成的 draft 先
      commit 再返回**,不白烧已付 token,收尾讲解阶段跳过);题开始前
      (克隆/预读)命中则抛 PracticeGenerateCancelled(此时无题可保留)。
      不传时与旧版行为一致。
    """
    if client is None:
        client = resolve_llm_client(db, task)

    # 用户练习设置:是否允许出题前恢复工作区;思考模式覆盖出题模型的思考开关
    settings_row = db.query(PracticeSettings).filter(
        PracticeSettings.user_id == user_id
    ).first()
    _apply_thinking_mode(client, settings_row)

    # 用户学习主题词表(仅启用主题参与分类与出题):
    # 停用主题的 finding 出题前被过滤,只停新增、不动存量。
    # CRUD 已保证启用数不归零;此处防御兜底为内置词表
    all_topics = ensure_user_topics(db, user_id)
    enabled_topics = [t for t in all_topics if t.enabled]
    if enabled_topics:
        topic_defs = [
            {"key": t.key, "name": t.name, "description": t.description}
            for t in enabled_topics
        ]
    else:
        logger.warning(
            "[practice] task=%s user=%s 无启用学习主题,按内置词表出题",
            task.id, user_id,
        )
        topic_defs = [
            {"key": d["key"], "name": d["name"], "description": d["description"]}
            for d in BUILTIN_TOPIC_DEFS
        ]
    custom_defs = [d for d in topic_defs if d["key"] not in
                   {b["key"] for b in BUILTIN_TOPIC_DEFS}]

    # 工作区:存活 → 挂工具循环;已清理且开了恢复开关 → 后台线程重新 clone,
    # 与主题分类(纯 LLM,不依赖工作区)并行进行 —— 大仓库克隆可能几分钟,
    # 串在分类前面用户只能干看(旧行为:“开始出题”日志在 clone 之后才打)
    task_id_str = str(task.id)
    probe = sandbox_tools.get_workspace_info(task_id_str)
    restore_in_bg = _workspace_restore_worth_bg(task, settings_row, probe)
    ws_box: dict[str, Any] = {}
    # cancel_check 仅非空时下传:存量测试按属性替身 _ensure_workspace
    # 不接这个参数,无停止需求时不传就不会 TypeError
    restore_kw: dict[str, Any] = (
        {} if should_stop is None else {"cancel_check": should_stop}
    )

    def _run_workspace_restore() -> None:
        try:
            ws_box["info"] = _ensure_workspace(
                db, task, settings_row,
                event_callback=event_callback,
                # Session 非线程安全:token 在主线程读好再带进线程
                git_tokens=ws_box.get("git_tokens") or {},
                **restore_kw,
            )
        except PracticeGenerateCancelled as e:
            # 用户停止出题:克隆已中断。线程里直接 raise 只会落到默认异常钩子,
            # 主线程 join 后照常往下跑并把"停止"误当成"没代码上下文",
            # 所以把取消原因存在盒子里交给主线程冒泡
            ws_box["cancelled"] = str(e) or "出题已停止"
        except Exception as e:
            logger.warning("[task=%s] 后台恢复工作区异常(降级为无工具出题): %s", task.id, e)
            ws_box["info"] = probe

    ws_thread: threading.Thread | None = None
    if restore_in_bg:
        ws_box["git_tokens"] = _load_git_tokens(db, task.user_id)
        ws_thread = threading.Thread(
            target=_run_workspace_restore, daemon=True,
            name=f"practice-restore-{task_id_str[:8]}",
        )
        ws_thread.start()

    # 选题:agent2 标记的学习点(practice_worthy)优先,不足补未标记的;
    # 无标记时与按 created_at 取前 N 条等价(向后兼容)
    findings = _select_findings(db, task, max_findings)
    total_findings = len(findings)

    # 主题自动匹配:规则先行 + LLM 批量兜底(每任务至多一次分类调用)
    topic_map = _match_finding_topics(task, findings, client, topic_defs)
    # 停止检查点:分类是一整次 LLM 调用,中途按下的停止在这里拦(尚未建题,
    # 直接冒取消而不是白做一次材料预读)
    if should_stop is not None and should_stop():
        raise PracticeGenerateCancelled("出题已停止(主题分类后,尚未生成题目)")
    # 主题开关过滤:规则捷径可能命中停用主题(如停用安全后 metadata 带 CWE 的
    # 发现),分类完成后统一拦截,并重算进度分母
    enabled_keys = {d["key"] for d in topic_defs}
    disabled_hits = [f.id for f in findings if topic_map[f.id] not in enabled_keys]
    if disabled_hits:
        logger.info(
            "[practice] task=%s 主题开关跳过 %d 条停用主题的 finding",
            task.id, len(disabled_hits),
        )
        findings = [f for f in findings if topic_map[f.id] in enabled_keys]
        topic_map = {f.id: topic_map[f.id] for f in findings}
        total_findings = len(findings)

    # 预读材料需要工作区路径:此处才等后台 clone 结束(分类/选题已与其重叠完成)
    if ws_thread is not None:
        ws_thread.join()
        if ws_box.get("cancelled"):
            # 克隆阶段被用户停止:此处尚未出题,直接冒取消给 job 层置 cancelled
            raise PracticeGenerateCancelled(str(ws_box["cancelled"]))
    ws_info = (
        ws_box.get("info") if ws_thread is not None
        else _ensure_workspace(
            db, task, settings_row, event_callback=event_callback, **restore_kw,
        )
    )
    repo_path = (ws_info or {}).get("repo_path") or ""

    # 题开始前的最后一个检查点(材料预读要逐文件读工作区,大仓库也能耗数十秒)
    if should_stop is not None and should_stop():
        raise PracticeGenerateCancelled("出题已停止(尚未生成题目)")

    # 材料预读:按发现的源码定位一次性读好(同一文件的多条 finding 共用读取),
    # 命中预读的 finding 改用「按需补读」提示词变体 → LLM 往返从 2~4 次压到 1 次
    materials = _prefetch_materials(task_id_str, repo_path, findings)
    if materials:
        logger.info(
            "[practice] task=%s 材料预读命中 %d/%d 条 finding",
            task.id, len(materials), total_findings,
        )

    # 预构建各主题的 system prompt(工作区可用性统一判定;
    # 内置主题用专有视角,自定义主题用通用模板 + 用户描述;
    # 已附材料的走「按需补读」工具段,未附的仍要求先调工具)
    prompt_cache: dict[tuple[str, bool], str] = {}

    def _system_prompt_for(topic: str, has_material: bool) -> str:
        ck = (topic, has_material)
        if ck not in prompt_cache:
            prompt_cache[ck] = build_system_prompt(
                topic,
                workspace_available=bool(repo_path),
                custom_topics=custom_defs,
                material_prefetched=has_material,
            )
        return prompt_cache[ck]

    # 出题起始快照:模型/主题分布/工作区/发现数(排查无题产出时的第一手上下文)
    logger.info(
        "[practice] 开始出题 task=%s user=%s model=%s topics=%s workspace=%s findings=%d",
        task.id, user_id, getattr(client, "model", "?"),
        _topic_stats(topic_map), bool(repo_path), total_findings,
    )

    # 已有 dedup_hash(同用户),避免重复入库
    existing_hashes = {
        row[0] for row in db.query(Question.dedup_hash).filter(
            Question.user_id == user_id
        ).all()
    }
    # finding 级短路集合:该发现本用户已出过题则不再走 LLM(force_regenerate 时为空集)
    already_generated: set[str] = set()
    if not force_regenerate:
        already_generated = {
            str(rid) for (rid,) in db.query(Question.source_result_id).filter(
                Question.user_id == user_id,
                Question.source_result_id.isnot(None),
            ).all()
        }
        _pre_skipped = sum(1 for f in findings if str(f.id) in already_generated)
        if _pre_skipped:
            logger.info(
                "[practice] task=%s %d/%d 条 finding 本用户已出过题,本轮不再付 LLM 成本",
                task.id, _pre_skipped, total_findings,
            )

    created: list[Question] = []
    skipped = 0
    fatal_reason = ""  # 非空表示遇到不可重试致命错误,需中止原因冒泡
    kp_cache: dict[str, KnowledgePoint] = {}
    # 收尾知识点讲解:开关开启时边出题边汇上下文素材(关掉时零开销)
    want_explain = bool(
        settings_row is not None
        and getattr(settings_row, "generate_explanation_with_questions", False)
    )
    explain_digests: dict[str, dict] = {}

    # 先剔除本用户已出过题的 finding(整条跳过的不计入 LLM 工作量)
    pending = [f for f in findings if str(f.id) not in already_generated]
    pre_skipped = len(findings) - len(pending)
    if pre_skipped:
        skipped += pre_skipped
        if progress_callback:
            progress_callback(pre_skipped, total_findings)
    concurrency = _effective_concurrency(settings_row, len(pending))
    if concurrency > 1:
        logger.info(
            "[practice] task=%s 并发出题 %d 路(%d 条 finding;并发>1 时不推 token 流)",
            task.id, concurrency, len(pending),
        )

    def _produce(finding: Result) -> _FindingWork:
        """一条 finding 的出题工作:拼 prompt → LLM 往返 → 解析 → 质量关卡

        只跑 LLM 与本地校验,不碰数据库 —— 可直接当线程池的 worker 单元用。
        并发>1 时不推 token/tool 事件:侧栏打字机流是「一次只在一条 finding 上」
        的假设,多路并行交错起来只会成一团乱码。
        """
        fid_str = str(finding.id)
        meta = finding.metadata_ or {}
        topic = topic_map[finding.id]
        snippets = materials.get(fid_str) or []
        system_prompt = _system_prompt_for(topic, bool(snippets))
        prompt = _build_finding_prompt(
            finding, meta, _render_material_section(snippets),
        )
        with _provider_gate(client):
            questions, content, fatal = _generate_for_finding(
                client, system_prompt, prompt, finding, meta,
                task_id_str, repo_path,
                None if concurrency > 1 else event_callback,
            )
        return _FindingWork(
            finding=finding, meta=meta, topic=topic, snippets=snippets,
            questions=questions, content=content, fatal=fatal,
        )

    def _consume(work: _FindingWork) -> None:
        """主线程落库(SQLAlchemy Session 非线程安全):去重 → 建知识点 → 追加 draft"""
        nonlocal skipped, fatal_reason
        finding = work.finding
        if work.fatal:
            fatal_reason = work.fatal
            return
        if not work.questions:
            skipped += 1
            logger.warning(
                "[practice] finding=%s 未能产出任何题目(共 %d 次尝试);"
                "最后一次 LLM 输出样例: %r",
                finding.id, PARSE_RETRY + 1, (work.content or "")[:300],
            )
            return

        dup_skipped = 0
        kp_questions: dict[str, list[dict]] = {}   # 本条 finding 在各知识点下产出的题
        kp_meta: dict[str, tuple[str, str]] = {}    # key → (展示名, 主题)
        # 溯源:知识点 Result.metadata_.source_review_item_id → Question.source_review_item_id
        # 校验该审查项真实存在再写入(避免悬空外键破坏入库)
        review_item_uuid = _resolve_source_review_item_id(db, finding.metadata_)
        for q in work.questions:
            dedup_hash = compute_dedup_hash(q["stem"], q["code_snippet"])
            if dedup_hash in existing_hashes:
                dup_skipped += 1
                continue
            existing_hashes.add(dedup_hash)

            kp = _get_or_create_knowledge_point(
                db, user_id, q["knowledge_key"], q["knowledge_name"],
                languages=q["languages"],
                learning_topic=work.topic,
                cache=kp_cache,
            )
            if want_explain:
                # 攒下「这道题当初基于什么材料/发现出出来的」,供收尾批量讲解
                kp_questions.setdefault(kp.key, []).append(q)
                kp_meta[kp.key] = (kp.name, work.topic)
            question = Question(
                user_id=user_id,
                source_task_id=task.id,
                source_result_id=finding.id,
                source_review_item_id=review_item_uuid,
                knowledge_point_id=kp.id,
                qtype=QuestionType(q["qtype"]),
                stem=q["stem"],
                code_snippet=q["code_snippet"],
                options=q["options"],
                answer_idx=q["answer_idx"],
                explanation=q["explanation"],
                difficulty=q["difficulty"],
                status=QuestionStatus.DRAFT,
                dedup_hash=dedup_hash,
                learning_topic=work.topic,  # 该题实际匹配的出题主题
                origin=q["origin"],
                source_file=q["source_file"],
                source_lines=q["source_lines"],
            )
            db.add(question)
            created.append(question)

        if dup_skipped:
            logger.info(
                "[practice] finding=%s 去重跳过 %d 题(同用户已有相同题目)",
                finding.id, dup_skipped,
            )

        if want_explain:
            for key, qs in kp_questions.items():
                name, kp_topic = kp_meta[key]
                add_digest(
                    explain_digests, key, name, kp_topic,
                    make_source_digest(
                        finding.title, finding.content, work.meta, work.snippets, qs,
                    ),
                )

    # 已处理条数(串行/并发共用):既喂 progress 口径,也用于下面的
    # 「是否真的少跑了活」判定(旧写法在并发分支里另起 finished,停止路径
    # 又不累加,导致日志与断点进度都少报)
    finished = 0
    if concurrency <= 1:
        # 串行路径(默认):逐条出题 + 逐条推 SSE 事件
        for idx, finding in enumerate(pending):
            # 停止检查点(逐条之间):未开始的 finding 不再付 LLM 成本,
            # 已生成的题目在循环后的 commit 里照常入库
            if should_stop is not None and should_stop():
                logger.info(
                    "[practice] task=%s 按用户请求停止出题:已处理 %d/%d 条",
                    task.id, finished, len(pending),
                )
                break
            if event_callback:
                try:
                    event_callback("finding", {
                        "index": pre_skipped + idx + 1,
                        "total": total_findings,
                        "title": finding.title,
                    })
                except Exception:
                    pass
            _consume(_produce(finding))
            finished += 1
            if progress_callback:
                progress_callback(
                    min(pre_skipped + finished, total_findings), total_findings,
                )
            if fatal_reason:
                break
    else:
        # 并行只跑 LLM;落库在主线程逐条做,结果与串行一致
        # (各 finding 的出题内容彼此独立,dedup/知识点写入始终单线程)
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix=f"practice-gen-{task_id_str[:8]}",
        ) as pool:
            futures = [pool.submit(_produce, f) for f in pending]
            # 已消费(落库或异常计入)的 future:停止时只能补收还没收过的那些
            handled: set[Any] = set()
            for fut in as_completed(futures):
                if should_stop is not None and should_stop():
                    # 先把已跑完的 worker 落库(结果已付 LLM 成本,不接收
                    # 等于白烧),再取消尚未起跑的;在途的随 with 退出自然结束
                    # 范围 = 所有已 done 且**尚未消费过**的 future,含 as_completed
                    # 刚产出的这条(旧写法用 `is not fut` 把它排除,每次停止恰好
                    # 白丢一道已出题);前几轮已落库的必须跳过,否则重复消费
                    for done_fut in futures:
                        if done_fut in handled or not done_fut.done():
                            continue
                        handled.add(done_fut)
                        try:
                            _consume(done_fut.result())
                        except Exception as e:
                            logger.warning(
                                "[practice] 停止时落库已到结果失败,跳过该条: %s", e,
                            )
                            skipped += 1
                        finished += 1
                        if progress_callback:
                            progress_callback(
                                min(pre_skipped + finished, total_findings),
                                total_findings,
                            )
                    for other in futures:
                        other.cancel()
                    logger.info(
                        "[practice] task=%s 按用户请求停止出题(并发):已处理 %d/%d 条",
                        task.id, finished, len(pending),
                    )
                    break
                try:
                    work = fut.result()
                except Exception as e:
                    # 单条 worker 意外异常不拖垮整个 job(LLM 层已自有重试,
                    # 走到这里的多是本地 bug):计一条未出题,继续处理其余
                    logger.exception("[practice] 并发出题 worker 异常,跳过该条: %s", e)
                    handled.add(fut)
                    skipped += 1
                    finished += 1
                    if progress_callback:
                        progress_callback(
                            min(pre_skipped + finished, total_findings), total_findings,
                        )
                    continue
                handled.add(fut)
                _consume(work)
                finished += 1
                if progress_callback:
                    progress_callback(
                        min(pre_skipped + finished, total_findings), total_findings,
                    )
                if fatal_reason:
                    # 额度/认证类错误:取消还没起跑的;在途的等它自然结束
                    for other in futures:
                        other.cancel()
                    break

    # 停止口径与 job 层的 is_cancelled 对齐:请求过停止**且确实少跑了活**才算
    # 提前停止。只看标志会让"停止请求落在最后一条之后"的那次收尾(知识点讲解、
    # 致命错误原因)被静默跳过,而 job 那边照样按 done 收口 —— 用户看到"已完成"
    # 却少了讲解,两头说法不一致。
    stopped_early = bool(
        should_stop is not None and should_stop()
        and pre_skipped + finished < total_findings
    )

    db.commit()
    for q in created:
        db.refresh(q)

    # 收尾批量更新知识点讲解:一次调用覆盖 ≤8 个知识点(不是每题一次),
    # 失败只记日志 —— 讲解是增益,不能拖垮已生成的题目
    explain_written = 0
    if want_explain and explain_digests and not stopped_early:
        try:
            explain_written = explain_knowledge_points(
                db, user_id, explain_digests,
                client=resolve_explain_client(db, user_id, settings_row, client),
                custom_defs=custom_defs,
                event_callback=event_callback,
            )
        except Exception as e:
            logger.warning(
                "[practice] task=%s 知识点讲解生成失败(题目不受影响): %s",
                task.id, e,
            )

    logger.info(
        "[practice] 出题结束 task=%s: 生成 %d 题, %d/%d 条 finding 未出题,"
        " 知识点讲解更新 %d 条%s",
        task.id, len(created), skipped, total_findings, explain_written,
        f"(致命错误中止: {fatal_reason})" if fatal_reason else "",
    )
    if stopped_early:
        # 用户停止且确实少跑了活:已生成的 draft 已在上面 commit,不抛致命错误;
        # 调用方按 jobs.is_cancelled(同一"少跑了活"口径)把 job 置 cancelled
        # 并展示部分结果
        logger.info(
            "[practice] task=%s 出题已停止:保留 %d 道已生成题(待确认)",
            task.id, len(created),
        )
        return created, skipped
    if fatal_reason:
        # 已生成的 draft 照常保留;错误原因冒泡给 job 层展示
        suffix = f",中止前已生成 {len(created)} 题" if created else ""
        raise PracticeGenerateError(f"{fatal_reason}{suffix}")
    return created, skipped
