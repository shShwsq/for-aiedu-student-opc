"""任务完成后自动归纳写入长期记忆(钩子)。

由 orchestrator 在任务成功完成段调用。失败兜底:任何异常都不影响任务完成状态。
借鉴 react_agent._llm_compress_history 的"关 thinking 加速 + 截断 + 降级"范式。

写入策略:
- 项目记忆:按 repo_url 归一化找到/创建 Project,把 LLM 归纳的 project_memory_update
  (结构化 list[{category,item}])按类别去重合并到 memory_content(## 类别 + - 条目格式)
- 全局记忆:同理把 global_memory_update 合并到 UserMemory.content
- 合并鲁棒:用户可经 /memory API 手改记忆(整体覆盖,自由文本),_merge_structured
  解析时保留所有内容——已知类别块去重合并新条目(块内非列表行原样保留),未知类别块
  与游离文本原样保留,不丢用户手写内容。超长先删 preamble 再尾部截断。

并发兜底:Project 表 UNIQUE(user_id, repo_url_normalized) 约束,并发 INSERT 抛
IntegrityError,catch 后 rollback 回查已建行。
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.memory_settings import (
    DEFAULT_MEMORY_INJECT_MAX_CHARS,
    DEFAULT_MEMORY_STRUCTURE_MODE,
    MemorySettings,
)
from app.models.practice import (
    THINKING_MODE_FOLLOW,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
)
from app.models.project import Project
from app.models.task import Conversation, Task
from app.models.user_llm_config import UserLLMConfig
from app.models.user_memory import UserMemory
from app.prompts.memory_curator import (
    DEFAULT_GLOBAL_CATEGORY_DEFS,
    DEFAULT_PROJECT_CATEGORY_DEFS,
    PROJECT_CATEGORIES,
    _SUMMARIZE_INJECT_PROMPT,
    _SUMMARIZE_PROMPT,
    _SUMMARIZE_PROMPT_FREEFORM,
    categories_titles,
    render_categories_with_desc,
)
from app.services.repo_url import normalize_repo_url

logger = logging.getLogger(__name__)

# 记忆合并后的存储上限(超长先删 Legacy 块,再尾部截断保留新内容)
# 项目记忆完整版写入沙箱文件供 agent 随时 read_file 查阅(无字数限制),故放宽到 50000
MAX_PROJECT_MEM_STORE = 50000
MAX_GLOBAL_MEM_STORE = 10000

# 精简版记忆注入 system prompt 的字符上限(超出调 LLM 精简,失败兜底硬截断)
MAX_PROJECT_MEM_INJECT = 2000


def load_memory_settings(db: Session, user_id) -> MemorySettings | None:
    """读取用户记忆设置行(只读,无行返回 None → 调用方用系统默认)。"""
    if user_id is None:
        return None
    try:
        return (
            db.query(MemorySettings)
            .filter(MemorySettings.user_id == user_id)
            .first()
        )
    except Exception as e:  # noqa: BLE001 - 读设置失败不阻断归纳,回退默认
        logger.warning(f"加载记忆设置失败(用默认): {e}")
        return None


def resolve_memory_llm_client(db: Session, user_id) -> tuple[LLMClient, str]:
    """解析记忆归纳/精简专用模型:MemorySettings.curator_llm_config_id > env 默认。

    独立于任务自带模型(不借用 task.llm_config_id):归纳是轻量任务,适合选便宜快的模型。
    返回 (client, source):source ∈ {"memory","env"}(日志/展示用)。
    """
    cfg_id: str | None = None
    if user_id is not None:
        try:
            cfg_row = (
                db.query(UserLLMConfig)
                .filter(UserLLMConfig.user_id == user_id)
                .first()
            )
            if cfg_row is not None:
                settings_row = load_memory_settings(db, user_id)
                cfg_id = settings_row.curator_llm_config_id if settings_row else None
                if cfg_id:
                    target = next(
                        (c for c in (cfg_row.llm_configs or []) if c.get("id") == cfg_id),
                        None,
                    )
                    if target is not None:
                        return LLMClient.from_config_dict(target), "memory"
                    logger.warning(
                        "[user=%s] 未找到记忆模型配置 id=%s,回退 env 默认",
                        user_id, cfg_id,
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("加载记忆模型配置失败,回退 env 默认: %s", e)
    return LLMClient(), "env"


def _chat(llm: LLMClient, prompt: str, thinking_mode: str, max_tokens: int = 2048) -> str:
    """单次流式调用收集文本;按 thinking_mode 覆盖模型思考开关并恢复。

    thinking_mode: follow=保持模型自身开关;on/off=强制开/关。
    """
    collected: list[str] = []
    original_thinking = llm.enable_thinking
    try:
        if thinking_mode == THINKING_MODE_OFF:
            llm.enable_thinking = False
        elif thinking_mode == THINKING_MODE_ON:
            llm.enable_thinking = True
        for chunk in llm.chat_stream(
            [{"role": "user", "content": prompt}], max_tokens=max_tokens,
        ):
            if chunk.content_delta:
                collected.append(chunk.content_delta)
            if chunk.finish_reason in ("stop", "length"):
                break
    finally:
        llm.enable_thinking = original_thinking
    return "".join(collected).strip()


def summarize_and_save_memory(
    task: Task, db: Session, client: LLMClient | None = None,
) -> None:
    """任务完成后归纳写入记忆(读用户「记忆设置」)。

    前置条件:task.status == COMPLETED(由调用方保证,失败任务不调)。
    user_id 为 None(匿名任务)或无 repo_url → 跳过。
    MemorySettings.memory_enabled=False → 跳过自动归纳(用户手改记忆不受影响)。
    任何异常都 catch + log,不影响任务完成。

    client 参数保留仅为向后兼容调用点签名;归纳模型经 resolve_memory_llm_client
    独立解析(记忆专用模型 > env 默认),不再借用任务自带模型。
    """
    if task.user_id is None:
        return
    try:
        settings_row = load_memory_settings(db, task.user_id)
        if settings_row is not None and not settings_row.memory_enabled:
            return
        mode = (
            settings_row.structure_mode if settings_row
            else DEFAULT_MEMORY_STRUCTURE_MODE
        )
        thinking_mode = settings_row.thinking_mode if settings_row else THINKING_MODE_FOLLOW
        inject_max = (
            settings_row.inject_max_chars if settings_row and settings_row.inject_max_chars
            else DEFAULT_MEMORY_INJECT_MAX_CHARS
        )

        params = task.params or {}
        repo_url = params.get("repo_url")
        if not repo_url:
            # 无仓库的任务不写项目记忆(MVP 跳过,避免无仓库归属的归纳)
            return

        react_summaries = _load_react_summaries_text(db, task.id)
        ua_reasoning = _load_final_ua_eval(db, task.id)
        if not react_summaries and not ua_reasoning:
            return

        llm, _mem_src = resolve_memory_llm_client(db, task.user_id)

        if mode == "freeform":
            _curate_freeform(
                task, db, llm, repo_url, react_summaries, ua_reasoning,
                thinking_mode, inject_max,
            )
        else:
            # 结构化模式:类别为用户自定义(标题+描述);空回退内置默认
            proj_defs = (
                settings_row.project_categories if settings_row else []
            ) or DEFAULT_PROJECT_CATEGORY_DEFS
            glob_defs = (
                settings_row.global_categories if settings_row else []
            ) or DEFAULT_GLOBAL_CATEGORY_DEFS
            _curate_structured(
                task, db, llm, repo_url, react_summaries, ua_reasoning,
                thinking_mode, inject_max, proj_defs, glob_defs,
            )
    except Exception as e:
        logger.warning(
            f"[task={task.id}] 归纳写入记忆失败(忽略,不影响任务完成): {e}"
        )


def _curate_structured(
    task, db, llm, repo_url, react_summaries, ua_reasoning,
    thinking_mode, inject_max, proj_defs, glob_defs,
) -> None:
    """structured 模式:LLM 按用户自定义类别(标题+描述)输出分类 JSON,按类别去重合并。

    proj_defs/glob_defs 为 [{title, description}];描述供提示词指导归类,
    标题列用于合并去重与精简模板(非法类别兜底归入末位类别)。
    """
    proj_cats = categories_titles(proj_defs)
    glob_cats = categories_titles(glob_defs)
    prompt = _SUMMARIZE_PROMPT.format(
        repo_url=repo_url,
        user_intent=(task.user_input or "")[:1000],
        react_summaries=react_summaries[:8000],
        ua_reasoning=ua_reasoning[:2000],
        project_categories=render_categories_with_desc(proj_defs),
        global_categories=render_categories_with_desc(glob_defs),
    )
    content = _chat(llm, prompt, thinking_mode)
    update = _parse_summary_json(content)
    if not update:
        return

    # 项目记忆按类别合并
    proj_items = _clean_items(update.get("project_memory_update"))
    if proj_items:
        proj = _get_or_create_project(db, task.user_id, repo_url)
        if proj is not None:
            existing = (proj.memory_content or "").strip()
            merged = _merge_structured(
                existing, proj_items, proj_cats, MAX_PROJECT_MEM_STORE,
            )
            if merged != existing:  # 有变化才写(完全去重则跳过)
                proj.memory_content = merged
                # 同步重新生成精简版(注入 system prompt 用)
                proj.memory_summary = generate_memory_summary(
                    merged, llm, max_chars=inject_max, thinking_mode=thinking_mode,
                    categories=proj_cats,
                )
                proj.last_summary_at = datetime.now(timezone.utc)
                db.commit()
                logger.info(
                    f"[task={task.id}] 已更新项目记忆 (project={proj.id})"
                )

    # 全局记忆按类别合并
    global_items = _clean_items(update.get("global_memory_update"))
    if global_items:
        mem = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == task.user_id)
            .first()
        )
        if mem is None:
            mem = UserMemory(user_id=task.user_id, content="")
            db.add(mem)
        existing = (mem.content or "").strip()
        merged = _merge_structured(
            existing, global_items, glob_cats, MAX_GLOBAL_MEM_STORE,
        )
        if merged != existing:
            mem.content = merged
            db.commit()
            logger.info(
                f"[task={task.id}] 已更新全局记忆 (user={task.user_id})"
            )


def _curate_freeform(
    task, db, llm, repo_url, react_summaries, ua_reasoning,
    thinking_mode, inject_max,
) -> None:
    """freeform 模式:LLM 输出两段纯文本,追加合并(不做类别去重)。"""
    prompt = _SUMMARIZE_PROMPT_FREEFORM.format(
        repo_url=repo_url,
        user_intent=(task.user_input or "")[:1000],
        react_summaries=react_summaries[:8000],
        ua_reasoning=ua_reasoning[:2000],
    )
    content = _chat(llm, prompt, thinking_mode)
    update = _parse_summary_json(content)
    if not update:
        return

    proj_text = _as_text(update.get("project_memory_update"))
    if proj_text:
        proj = _get_or_create_project(db, task.user_id, repo_url)
        if proj is not None:
            existing = (proj.memory_content or "").strip()
            merged = _merge_freeform(existing, proj_text, MAX_PROJECT_MEM_STORE)
            if merged != existing:
                proj.memory_content = merged
                proj.memory_summary = generate_memory_summary(
                    merged, llm, max_chars=inject_max, thinking_mode=thinking_mode,
                    structured=False,
                )
                proj.last_summary_at = datetime.now(timezone.utc)
                db.commit()
                logger.info(
                    f"[task={task.id}] 已更新项目记忆(freeform, project={proj.id})"
                )

    global_text = _as_text(update.get("global_memory_update"))
    if global_text:
        mem = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == task.user_id)
            .first()
        )
        if mem is None:
            mem = UserMemory(user_id=task.user_id, content="")
            db.add(mem)
        existing = (mem.content or "").strip()
        merged = _merge_freeform(existing, global_text, MAX_GLOBAL_MEM_STORE)
        if merged != existing:
            mem.content = merged
            db.commit()
            logger.info(
                f"[task={task.id}] 已更新全局记忆(freeform, user={task.user_id})"
            )


# ============================================================
# 精简版记忆生成(注入 system prompt 用)
# ============================================================


def generate_memory_summary(
    memory_content: str, llm: LLMClient | None,
    *,
    max_chars: int = MAX_PROJECT_MEM_INJECT,
    thinking_mode: str = THINKING_MODE_OFF,
    structured: bool = True,
    categories: list[str] | None = None,
) -> str:
    """生成精简版项目记忆(注入 system prompt 用,≤max_chars 字符)。

    策略(兼顾质量与成本):
    - memory_content 为空 → 返回 ""
    - len ≤ max_chars → 直接用完整内容(零成本,无需 LLM)
    - structured=False(freeform 模式)→ 跳过 LLM,直接硬截断(散文不拆类别)
    - 否则调 LLM 精简,保留 Hard Constraints/Known Issues 优先
    - LLM 不可用 / 调用失败 / 返回空 → 回退硬截断 memory_content[:max_chars]

    llm 为 None 时(如 PUT 路由未加载用户 LLM)直接走硬截断回退。
    thinking_mode 控制精简调用的思考开关(follow/on/off;默认 off 保持旧行为)。
    categories 为注入模板要输出的类别序列(按 structure_mode 传入,默认完整枚举)。
    """
    content = (memory_content or "").strip()
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    if not structured:
        # freeform 散文:不拆类别,超长直接保留头部(约束类相对在前)
        return content[:max_chars]
    if llm is None:
        # 无 LLM 可用,直接硬截断(保尾部新内容相对更重要,但这里取头部:
        # 完整记忆按类别排序,Hard Constraints 在头部,故保留头部更合理)
        return content[:max_chars]

    inject_categories = ", ".join(categories or PROJECT_CATEGORIES)
    try:
        prompt = _SUMMARIZE_INJECT_PROMPT.format(
            max_chars=max_chars,
            memory_content=content,
            inject_categories=inject_categories,
        )
        # max_tokens 随 max_chars 缩放:英文≈chars/4 token、CJK≈1 token/char,
        # 取 max(2048, max_chars) 保证大注入上限不被输出长度提前截断。
        summary = _chat(llm, prompt, thinking_mode, max_tokens=max(2048, max_chars))

        if not summary:
            return content[:max_chars]
        # LLM 可能超长,回退截断
        if len(summary) > max_chars:
            summary = summary[:max_chars]
        return summary
    except Exception as e:
        logger.warning(f"LLM 生成精简记忆失败,回退硬截断: {e}")
        return content[:max_chars]


# ============================================================
# 辅助函数
# ============================================================


def _as_text(val) -> str:
    """容错取文本:仅当为 str 时 strip 返回;list/dict 等误返回按空处理。

    freeform 模式下归纳模板要求字符串,但模型偶尔会回吐结构化数组——
    直接 .strip() 会抛 AttributeError 使整次归纳(项目+全局)被外层 catch 后全部跳过。
    """
    return val.strip() if isinstance(val, str) else ""


def _clean_items(raw) -> list[dict]:
    """清洗 LLM 输出的条目列表:过滤非法结构,规范化 category。

    - raw 必须是 list,每项是 dict 且含非空 item 字符串。
    - category 不在枚举内(由调用方通过 categories 校验)时,这里只做基本清洗,
      category 的兜底归入在 _merge_structured 中处理。
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        item_text = (it.get("item") or "").strip()
        cat = (it.get("category") or "").strip()
        if not item_text or not cat:
            continue
        cleaned.append({"category": cat, "item": item_text})
    return cleaned


def _load_react_summaries_text(db: Session, task_id) -> str:
    """加载本任务 react_agent 各轮总结,拼成文本。

    查 Conversation(role=react_agent, type=thinking),按 round_idx 升序,
    每轮取最后一条 content 作为该轮 summary。
    借鉴 orchestrator._load_react_summaries 的查询逻辑。
    """
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "agent1",
            Conversation.type == "thinking",
        )
        .order_by(Conversation.round_idx.asc(), Conversation.created_at.asc())
        .all()
    )
    summaries_by_round: dict[int, str] = {}
    for c in convs:
        if c.content:
            summaries_by_round[c.round_idx] = c.content
    if not summaries_by_round:
        return ""
    parts = []
    for ridx in sorted(summaries_by_round.keys()):
        parts.append(f"### Round {ridx} react_agent summary\n{summaries_by_round[ridx]}")
    return "\n\n".join(parts)


def _load_final_ua_eval(db: Session, task_id) -> str:
    """加载 agent2 最终评估文本。

    优先查 role=agent2, type=summary 的最后一条 content(含"最终评估");
    若无 summary,回退到最后一条 type=evaluation 的 reasoning(含 covered/missing/判断)。
    """
    summary = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "agent2",
            Conversation.type == "summary",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if summary and summary.content:
        return summary.content

    ev = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "agent2",
            Conversation.type == "evaluation",
        )
        .order_by(
            Conversation.round_idx.desc(), Conversation.created_at.desc()
        )
        .first()
    )
    if ev:
        return ev.reasoning or ev.content or ""
    return ""


def _parse_summary_json(content: str) -> dict | None:
    """解析 LLM 输出的归纳 JSON,容忍 markdown 包裹。

    期望结构:
    {{"project_memory_update": [{{"category":..., "item":...}}, ...],
      "global_memory_update": [...]}}
    解析失败返回 None(调用方跳过写入)。条目清洗由 _clean_items 负责。
    """
    text = content.strip()
    if not text:
        return None
    # 去掉 markdown 代码块包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            f"归纳 JSON 解析失败(跳过写入): {e}, raw: {content[:300]}"
        )
    return None


def _get_or_create_project(
    db: Session, user_id, repo_url: str,
) -> Project | None:
    """按归一化 repo_url 查 Project,无则建。

    并发兜底:UNIQUE(user_id, repo_url_normalized) 约束下,并发 INSERT 会抛
    IntegrityError,catch 后 rollback 并回查已建行。
    """
    norm = normalize_repo_url(repo_url)
    if not norm:
        return None

    proj = (
        db.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.repo_url_normalized == norm,
        )
        .first()
    )
    if proj is not None:
        return proj

    new_proj = Project(
        user_id=user_id,
        repo_url_normalized=norm,
        repo_url_raw=repo_url,
        alias=None,
        note=None,
        memory_content="",
    )
    db.add(new_proj)
    try:
        db.commit()
        db.refresh(new_proj)
        return new_proj
    except IntegrityError:
        # 并发:另一线程已建同样 repo_url,回查
        db.rollback()
        return (
            db.query(Project)
            .filter(
                Project.user_id == user_id,
                Project.repo_url_normalized == norm,
            )
            .first()
        )


def _parse_structured(text: str) -> tuple[str, list[tuple[str, str]]]:
    """解析 ## 类别 格式,保留所有内容(容忍用户手改)。

    返回 (preamble, segments):
    - preamble: 第一个 ## 标题前的游离文本(strip 后,可能为 "")。无 ## 标题时
      整个文本作为 preamble(用户自由笔记/旧格式残留都原样保留,不丢)。
    - segments: [(category_name, body_text), ...],body_text 为标题下到下一标题
      间的原文(含 - 条目和用户写的任何其它行,已 strip)。
    """
    lines = text.splitlines()
    preamble_lines: list[str] = []
    segments: list[tuple[str, str]] = []
    current_name: str | None = None
    current_body: list[str] = []
    seen_header = False

    for line in lines:
        if line.startswith("## "):
            if current_name is not None:
                segments.append((current_name, "\n".join(current_body)))
            current_name = line[3:].strip()
            current_body = []
            seen_header = True
        elif seen_header:
            current_body.append(line)
        else:
            preamble_lines.append(line)

    if current_name is not None:
        segments.append((current_name, "\n".join(current_body)))

    preamble = "\n".join(preamble_lines).strip()
    return preamble, segments


def _merge_structured(
    existing: str,
    new_items: list[dict],
    categories: list[str],
    max_chars: int,
) -> str:
    """按类别合并结构化记忆条目(鲁棒:保留用户手改的所有内容,不丢)。

    解析 existing:
    - ## 类别 块:已知类别提取 - 条目与新条目去重合并,块内用户写的非列表行原样保留;
      未知类别(用户自定义)块整体原样保留。
    - 无 ## 标题的游离文本(用户自由笔记/旧格式残留)作为 preamble 原样保留。

    新条目按 category 归入对应已知类别块(非法 category 兜底归入 末位类别,即列表最后一类)。
    重写顺序: preamble → 已知类别(按枚举顺序)→ 未知类别(按原顺序)。
    超长处理:先删 preamble(相对最旧),再尾部截断保留尾部新内容。
    """
    existing_stripped = (existing or "").strip()
    preamble, segments = (
        _parse_structured(existing_stripped) if existing_stripped else ("", [])
    )

    # 已知类别 -> 首次出现的 segment 索引(用户重复写的同名块取第一个)
    seg_index: dict[str, int] = {}
    for i, (name, _) in enumerate(segments):
        if name not in seg_index:
            seg_index[name] = i

    # 合并新条目到已知类别块(同块内 - 条目精确去重,非列表行不动)
    for it in new_items:
        cat = it["category"]
        if cat not in categories:
            # 非法类别归入末位类别(约定列表最后一类为杂项桶)
            cat = categories[-1] if categories else "Lessons Learned"
        item_text = it["item"]
        if cat in seg_index:
            idx = seg_index[cat]
            name, body_text = segments[idx]
            # strip 去掉块首尾空行(块间空行归入前块 body 的副作用),保留块内中间结构
            body_lines = body_text.strip().split("\n") if body_text.strip() else []
            existing_items = {
                ln[2:].strip() for ln in body_lines if ln.startswith("- ")
            }
            if item_text not in existing_items:
                body_lines.append(f"- {item_text}")
                segments[idx] = (name, "\n".join(body_lines))
        else:
            segments.append((cat, f"- {item_text}"))
            seg_index[cat] = len(segments) - 1

    # 重写:preamble → 已知类别(枚举顺序)→ 未知类别(原顺序)
    parts: list[str] = []
    if preamble:
        parts.append(preamble)

    written: set[str] = set()
    for cat in categories:
        if cat in seg_index:
            _, body_text = segments[seg_index[cat]]
            body_text = body_text.strip()
            if body_text:
                parts.append(f"## {cat}\n{body_text}")
            written.add(cat)

    for name, body_text in segments:
        if name in written:
            continue
        body_text = body_text.strip()
        if body_text:
            parts.append(f"## {name}\n{body_text}")
        written.add(name)

    merged = "\n\n".join(parts)
    if len(merged) <= max_chars:
        return merged

    # 超长:先删 preamble(游离文本相对最旧)
    if preamble and len(parts) > 1:
        candidate = "\n\n".join(parts[1:])
        if len(candidate) <= max_chars:
            return candidate
        merged = candidate

    # 仍超长:尾部截断(保留尾部新内容)
    return "[...truncated...]\n" + merged[-(max_chars - 30):]


def _merge_freeform(existing: str, new_text: str, max_chars: int) -> str:
    """合并自由叙述记忆(freeform 模式):旧内容 + 新段落拼接,不做类别去重。

    - new_text 为空 → 原样返回 existing(无变化)
    - new_text 已作为子串包含于 existing → 返回 existing(近似去重,跳过写入)
    - 否则拼接(existing 在前,新内容在后);超长时删旧内容头部保留尾部新内容
      (旧叙述相对更旧,新归纳相对更新鲜)
    保留用户手写内容:existing(含用户手改)整段原样拼在前,不丢。
    """
    new_text = (new_text or "").strip()
    existing_stripped = (existing or "").strip()
    if not new_text:
        return existing_stripped
    if existing_stripped and new_text in existing_stripped:
        return existing_stripped
    merged = (
        f"{existing_stripped}\n\n{new_text}" if existing_stripped else new_text
    )
    if len(merged) <= max_chars:
        return merged
    # 超长:删头部旧内容,保留尾部新内容
    return "[...truncated...]\n" + merged[-(max_chars - 30):]
