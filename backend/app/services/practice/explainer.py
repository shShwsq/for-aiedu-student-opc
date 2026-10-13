"""知识点讲解生成:任务侧为主、练习侧兜底,为知识点写/同步讲解

两条来源路径(均为"带上下文"而不是只给 key/name 让模型背概念):
- 任务侧驱动(sync_knowledge_explanations,出题收尾触发):
  单贡献任务直接采用 agent2 写的 Result.content(零额外 LLM);
  跨任务(≥2)调一次 LLM 把各任务解释合并为通用看板讲解;
  无任务来源(对话降级伪发现/老数据)不在收尾跑 LLM,交给按需路径。
- 看板按需(explain_knowledge_points,用户点「生成/更新讲解」):
  由 DB 重建上下文(该知识点最近的题目 + 题目关联的发现原文)重建讲解,
  存量知识点也能回填。

成本控制:一次 LLM 调用最多覆盖 MAX_KP_PER_BATCH 个知识点(超出分批),
不是每个知识点一次往返;出题收尾只在跨任务合并时才产生 LLM 调用。

覆盖规则:explanation_source=manual(用户自己写的总结)永不被自动写入;
任务侧驱动的幂等靠 explanation_contributor_tasks 集合判定(详见 sync)。

失败策略:任何异常只记日志,不抛出到调用方 —— 讲解是增益,不能拖垮出题。
"""
import logging
from datetime import datetime, timezone

from json_repair import repair_json
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.practice import (
    EXPLANATION_SOURCE_AUTO,
    EXPLANATION_SOURCE_MANUAL,
    MAX_EXPLANATION_CHARS,
    KnowledgePoint,
    Question,
)
from app.models.task import Result, Task
from app.prompts.practice import (
    build_explain_system_prompt,
    build_explain_user_prompt,
    build_merge_explain_system_prompt,
    build_merge_explain_user_prompt,
)
from app.services.practice.parse_utils import salvage_json_array

logger = logging.getLogger(__name__)

# 单批知识点数(超出分批,防单次输出被 max_tokens 截断且便于定位失败批次)
# 6 个 × 400 字中文 ≈ 2400 字符 + Markdown/JSON 转义 ≈ 2000-3000 tokens,
# 4096 上限留一倍余量;再往上加批次大小截断风险陡增(cl100k 类 tokenizer 更明显)
MAX_KP_PER_BATCH = 6
# 每个知识点最多带几条发现上下文(同知识点反复出题时取最新的几条)
MAX_SOURCES_PER_KP = 3
# 每条上下文最多带几道题
MAX_QUESTIONS_PER_SOURCE = 3
# 由 DB 重建上下文时,每个知识点最多取几道题
MAX_QUESTIONS_PER_KP = 6
# 素材字段截断长度(控住批量 prompt 体积)
_FINDING_CONTENT_CHARS = 1200
_MATERIAL_CHARS = 900
_QUESTION_EXPLANATION_CHARS = 300

# 讲解输出上限:段数可变(两必选 + 两可选→最多 4 段),按 MAX_KP_PER_BATCH=6 个知识点、每个软目标 200~600 字
# 估算,主流中文 tokenizer(0.6-1.0 token/字)约 1500-3000 token,4096 留一倍余量
_MAX_OUTPUT_TOKENS = 4096


def _clip(text: object, limit: int) -> str:
    return str(text or "").strip()[:limit]


def _field(obj, name: str, default=None):
    """dict 与 ORM 行两种形态统一取值(素材既来自出题管线的 dict 也来自 Question 行)"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def make_source_digest(
    finding_title: str, finding_content: str, metadata: dict | None,
    materials: list[dict] | None, questions: list[dict],
) -> dict:
    """一条发现在某知识点上的一份上下文(发现原文 + 真实材料 + 题目)"""
    meta = metadata or {}
    # 只带对讲解有用的元信息(全量 metadata 会把无关字段也喂进模型);
    # source_review_item_id 让兜底路径能回指原始审查项(可溯源证据)
    keep = {
        k: v for k, v in meta.items()
        if k in ("cwe", "severity", "file_path", "line_range", "line",
                 "learning_note", "origin", "verified", "suggestion",
                 "source_review_item_id")
    }
    return {
        "finding_title": _clip(finding_title, 200),
        "finding_content": _clip(finding_content, _FINDING_CONTENT_CHARS),
        "metadata": keep,
        "materials": [
            {
                "path": m.get("path"),
                "lines": m.get("lines"),
                "content": _clip(m.get("content"), _MATERIAL_CHARS),
            }
            for m in (materials or [])
        ],
        "questions": [_question_brief(q) for q in questions[:MAX_QUESTIONS_PER_SOURCE]],
    }


def _question_brief(q) -> dict:
    """题目摘要(给模型看的形态:题干/材料/选项/正确项/解析/出处)

    入参可以是出题管线里的 dict,也可以是 DB 路径的 Question 行(由 _field 统一取值);
    用「正确项文本」而不是下标,模型不必再猜答案位置。
    """
    options = [str(o) for o in (_field(q, "options") or [])]
    idx = _field(q, "answer_idx") or 0
    answer = options[idx] if 0 <= idx < len(options) else ""
    source = f"{_field(q, 'source_file') or ''} {_field(q, 'source_lines') or ''}".strip() or None
    return {
        "stem": _clip(_field(q, "stem"), 400),
        "material": _clip(_field(q, "code_snippet"), _MATERIAL_CHARS),
        "options": [_clip(o, 200) for o in options],
        "answer": _clip(answer, 200),
        "explanation": _clip(_field(q, "explanation"), _QUESTION_EXPLANATION_CHARS),
        "origin": _field(q, "origin") or "repo",
        "source": source,
    }


def add_digest(
    digests: dict[str, dict], knowledge_key: str, knowledge_name: str,
    learning_topic: str, source: dict,
) -> None:
    """把一份上下文归到某知识点的素材集合下(超出上限时丢弃最旧的)"""
    entry = digests.setdefault(knowledge_key, {
        "knowledge_key": knowledge_key,
        "knowledge_name": knowledge_name,
        "learning_topic": learning_topic,
        "sources": [],
    })
    # 主题与展示名以最新一次出题为准(题目落库时 first-wins,讲解素材用当前形态即可)
    entry["learning_topic"] = learning_topic or entry["learning_topic"]
    if knowledge_name:
        entry["knowledge_name"] = knowledge_name
    sources = entry["sources"]
    if len(sources) >= MAX_SOURCES_PER_KP:
        sources.pop(0)
    sources.append(source)


def build_digests_from_db(
    db: Session, user_id, knowledge_keys: list[str],
) -> dict[str, dict]:
    """为存量知识点从 DB 重建出题上下文(看板「生成讲解」按需路径)

    素材 = 该知识点最近的题目(含 code_snippet 与源码定位,这就是当时出题
    依据的真实材料)+ 题目关联的审计发现原文(title/content/metadata)。
    工作区已清理、预读片段没落库时,材料由 code_snippet 承担 —— 不虚构。
    """
    if not knowledge_keys:
        return {}
    kps = (
        db.query(KnowledgePoint)
        .filter(
            KnowledgePoint.user_id == user_id,
            KnowledgePoint.key.in_(knowledge_keys),
        )
        .all()
    )
    if not kps:
        return {}
    kp_by_id = {kp.id: kp for kp in kps}
    questions = (
        db.query(Question)
        .filter(
            Question.user_id == user_id,
            Question.knowledge_point_id.in_(list(kp_by_id)),
            # 归档题也带上:讲解要覆盖用户实际见过的全部考点
        )
        .order_by(Question.created_at.desc())
        .all()
    )
    # 逐知识点截断,避免一个热门知识点吃掉整批配额
    per_kp: dict[str, list[Question]] = {}
    for q in questions:
        kp = kp_by_id.get(q.knowledge_point_id)
        if kp is None:
            continue
        bucket = per_kp.setdefault(kp.key, [])
        if len(bucket) < MAX_QUESTIONS_PER_KP:
            bucket.append(q)

    # 关联的审计发现(一次查完,给模型「这条题当初是从哪个真实发现改编的」)
    result_ids = {q.source_result_id for qs in per_kp.values() for q in qs if q.source_result_id}
    findings: dict[str, object] = {}
    if result_ids:
        for r in db.query(Result).filter(Result.id.in_(list(result_ids))).all():
            # 按字符串索引:下面分组时已把 source_result_id 转成 str(UUID 对象不能直接对上)
            findings[str(r.id)] = r

    digests: dict[str, dict] = {}
    for kp in kps:
        qs = per_kp.get(kp.key) or []
        if not qs:
            continue  # 无题可依据 → 不生成(避免模型凭空编)
        by_result: dict[str, list[Question]] = {}
        for q in qs:
            by_result.setdefault(str(q.source_result_id or ""), []).append(q)
        for rid, group in by_result.items():
            finding = findings.get(rid) if rid else None
            source = make_source_digest(
                finding_title=finding.title if finding else "",
                finding_content=finding.content if finding else "",
                metadata=(finding.metadata_ if finding else None),
                materials=[],  # 预读片段未落库,由题目 code_snippet 承担材料角色
                questions=group,
            )
            add_digest(digests, kp.key, kp.name, kp.learning_topic, source)
    return digests


def _should_write(kp: KnowledgePoint, force: bool) -> bool:
    """覆盖判定:manual 永不覆盖;已有讲解只在 force 时重写"""
    if (kp.explanation_source or "") == EXPLANATION_SOURCE_MANUAL:
        return False
    if force:
        return True
    return not (kp.explanation or "").strip()


def _parse_explanations(content: str) -> dict[str, str]:
    """解析讲解输出 → {knowledge_key: markdown};容错同出题(散文包 JSON 先挽救)"""
    text = (content or "").strip()
    if not text:
        return {}
    try:
        parsed = repair_json(text, return_objects=True)
    except Exception:
        parsed = None
    if isinstance(parsed, str) or parsed is None:
        salvaged = salvage_json_array(text)
        try:
            parsed = repair_json(salvaged, return_objects=True) if salvaged else None
        except Exception:
            parsed = None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return {}
    out: dict[str, str] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        key = str(item.get("knowledge_key") or "").strip()
        markdown = str(item.get("markdown") or "").strip()
        if key and markdown:
            out[key] = markdown
    return out


def _collect_llm_text(client: LLMClient, messages: list[dict]) -> str:
    """一次性收集讲解输出(讲解不需要工具,也不推送打字机事件)

    遇到 finish_reason='length'(max_tokens 截断)记 warning:JSON 数组被切
    到一半时 json_repair 通常只能救回头部 KP,尾部会静默缺失,给排障留条线索。
    """
    parts: list[str] = []
    finish_reason = None
    for chunk in client.chat_stream(messages, max_tokens=_MAX_OUTPUT_TOKENS):
        if chunk.content_delta:
            parts.append(chunk.content_delta)
        if getattr(chunk, "finish_reason", None):
            finish_reason = chunk.finish_reason
    if finish_reason == "length":
        logger.warning(
            "[explain] 讲解输出被 max_tokens=%d 截断,尾部知识点可能缺失",
            _MAX_OUTPUT_TOKENS,
        )
    return "".join(parts)


def explain_knowledge_points(
    db: Session, user_id, digests: dict[str, dict], *,
    client: LLMClient,
    force: bool = False,
    custom_defs: list[dict] | None = None,
    event_callback=None,
) -> int:
    """批量生成/更新知识点讲解,返回实际写入的知识点数

    一次调用最多覆盖 MAX_KP_PER_BATCH 个知识点(超出分批);
    单批失败只跳过该批,不影响其它批次与调用方主流程。
    event_callback 收到 explain 阶段事件(start/done/failed),供出题侧栏展示。
    """
    if not digests:
        return 0
    keys = list(digests)
    kp_by_key = {
        kp.key: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == user_id,
            KnowledgePoint.key.in_(keys),
        ).all()
    }
    targets = [
        key for key in keys
        if key in kp_by_key and _should_write(kp_by_key[key], force)
    ]
    missing = [key for key in keys if key not in kp_by_key]
    if missing:
        logger.info("[explain] user=%s %d 个知识点不存在,跳过: %s", user_id, len(missing), missing[:5])
    if not targets:
        logger.info("[explain] user=%s 无待生成讲解(全部已有或为手工编辑)", user_id)
        return 0

    model_name = getattr(client, "model", "") or ""
    written = 0
    batches = [
        targets[i:i + MAX_KP_PER_BATCH]
        for i in range(0, len(targets), MAX_KP_PER_BATCH)
    ]
    _emit(event_callback, "start", total=len(targets), batches=len(batches), model=model_name)
    for batch_keys in batches:
        try:
            written += _explain_one_batch(
                db, user_id, kp_by_key, digests, batch_keys,
                client=client, custom_defs=custom_defs, model_name=model_name,
            )
        except Exception as e:
            # 单批失败(限流/超时/输出非法)不阻断其它批次,更不牵连出题
            logger.warning(
                "[explain] user=%s 批次失败(keys=%s): %s", user_id, batch_keys[:3], e,
            )
    db.commit()
    logger.info(
        "[explain] user=%s 讲解生成结束: 写入 %d/%d 个知识点(model=%s)",
        user_id, written, len(targets), model_name,
    )
    _emit(event_callback, "done", written=written, total=len(targets))
    return written


def _explain_one_batch(
    db: Session, user_id, kp_by_key: dict, digests: dict,
    batch_keys: list[str], *,
    client: LLMClient,
    custom_defs: list[dict] | None,
    model_name: str,
) -> int:
    """一批知识点的讲解生成:拼提示词 → 调模型 → 解析 → 落库(不 commit)"""
    items = [digests[k] for k in batch_keys]
    topics = list(dict.fromkeys(
        (i.get("learning_topic") or "") for i in items if i.get("learning_topic")
    ))
    messages = [
        {"role": "system", "content": build_explain_system_prompt(topics, custom_defs)},
        {"role": "user", "content": build_explain_user_prompt(items)},
    ]
    content = _collect_llm_text(client, messages)
    parsed = _parse_explanations(content)
    if not parsed:
        logger.warning(
            "[explain] user=%s 本批 %d 个知识点无有效讲解输出,样例: %r",
            user_id, len(batch_keys), (content or "")[:200],
        )
        return 0
    now = datetime.now(timezone.utc)
    written = 0
    for key in batch_keys:
        markdown = parsed.get(key)
        if not markdown:
            continue
        kp = kp_by_key[key]
        # 快速筛掉批次开始前就已标 manual 的(避免白跑 UPDATE);
        # 但 LLM 调用期间用户并发 PUT 的 manual 状态读不到(session 身份映射陈旧),
        # 真正的守卫交给下面的条件 UPDATE:让 DB 层原子判定 source != 'manual'
        if not _should_write(kp, force=True):
            continue
        new_explanation = markdown[:MAX_EXPLANATION_CHARS]
        result = db.execute(
            update(KnowledgePoint)
            .where(
                KnowledgePoint.id == kp.id,
                KnowledgePoint.explanation_source != EXPLANATION_SOURCE_MANUAL,
            )
            .values(
                explanation=new_explanation,
                explanation_source=EXPLANATION_SOURCE_AUTO,
                explanation_model=model_name[:64],
                explanation_updated_at=now,
            )
        )
        if not getattr(result, "rowcount", 0):
            # 并发下用户刚手工编辑过 → SQL WHERE 挡下,不覆盖 manual
            continue
        # 条件 UPDATE 绕过了 ORM 脏标记,手动同步身份映射,
        # 避免调用方后续读同一个 kp 拿到陈旧值
        kp.explanation = new_explanation
        kp.explanation_source = EXPLANATION_SOURCE_AUTO
        kp.explanation_model = model_name[:64]
        kp.explanation_updated_at = now
        written += 1
    db.flush()
    return written


# ============================================================
# 任务侧驱动的看板讲解(出题收尾采用/跨任务合并)
# ============================================================

# 单任务采用时 explanation_model 的取值(未经模型,标记来源为任务侧);
# 看板 badge 按 explanation_contributor_tasks 数量区分「来自任务/合并」
_ADOPTED_MODEL = "task_side"


def _is_knowledge_result(r: Result) -> bool:
    """Result 是否为知识点行(而非 agent1 summary 临时行)

    知识点 Result 的 metadata 由 agent2 整理时写入(learning_note /
    practice_worthy / learning_topic 至少其一);临时汇总行 metadata=None。
    """
    meta = r.metadata_
    return isinstance(meta, dict) and any(
        k in meta for k in ("learning_note", "practice_worthy", "learning_topic")
    )


def _task_contributions(db: Session, kp: KnowledgePoint) -> list[dict]:
    """该 KP 的贡献任务及各自的任务侧讲解(采用/合并规则的依据)

    取数优先走 results.knowledge_point_id 正式链接(出题收尾回写,题目
    删除/归档仍稳健);链接列为 NULL 的老数据回落 Question.source_result_id。
    同一任务取最新非空正文(多轮审查可能留多条同 key 知识点)。
    对话降级伪发现无 Result 行 → 自然为空(落「无任务来源」分支)。
    返回 [{task_id, task_title, explanation}],按任务首次参与顺序。
    """
    rows = (
        db.query(Result)
        .filter(Result.knowledge_point_id == kp.id)
        .order_by(Result.created_at)
        .all()
    )
    if not rows:
        rids = [
            rid
            for (rid,) in db.query(Question.source_result_id).filter(
                Question.knowledge_point_id == kp.id,
                Question.source_result_id.isnot(None),
            ).all()
        ]
        if rids:
            rows = (
                db.query(Result)
                .filter(Result.id.in_(rids))
                .order_by(Result.created_at)
                .all()
            )
    by_task: dict[str, str] = {}  # task_id -> 讲解(created_at 升序逐条覆盖 → 最新胜出)
    order: list[str] = []
    task_ids: list = []  # 原始 UUID 形态,供 Task 标题查询(不做 str 往返解析)
    for r in rows:
        if r.task_id is None or not _is_knowledge_result(r):
            continue
        text = (r.content or "").strip()
        if not text:
            continue
        tid = str(r.task_id)
        if tid not in by_task:
            order.append(tid)
            task_ids.append(r.task_id)
        by_task[tid] = text
    if not order:
        return []
    titles: dict[str, str] = {}
    for t in db.query(Task).filter(Task.id.in_(task_ids)).all():
        titles[str(t.id)] = (t.title or (t.user_input or "")[:60] or "").strip()
    return [
        {"task_id": tid, "task_title": titles.get(tid, ""), "explanation": by_task[tid]}
        for tid in order
    ]


def _write_explanation(
    db: Session, kp: KnowledgePoint, text: str, model_name: str,
    contributor_ids: list[str], now: datetime,
) -> bool:
    """讲解落库(含贡献任务集合);DB 层原子守卫 manual,并发手工编辑不覆盖"""
    new_explanation = (text or "").strip()[:MAX_EXPLANATION_CHARS]
    if not new_explanation:
        return False
    result = db.execute(
        update(KnowledgePoint)
        .where(
            KnowledgePoint.id == kp.id,
            KnowledgePoint.explanation_source != EXPLANATION_SOURCE_MANUAL,
        )
        .values(
            explanation=new_explanation,
            explanation_source=EXPLANATION_SOURCE_AUTO,
            explanation_model=model_name[:64],
            explanation_updated_at=now,
            explanation_contributor_tasks=list(contributor_ids),
        )
    )
    if not getattr(result, "rowcount", 0):
        return False  # 并发下用户刚改成 manual → 挡下,也不回写贡献集合
    # 条件 UPDATE 绕过了 ORM 脏标记,手动同步身份映射避免后续读到陈值
    kp.explanation = new_explanation
    kp.explanation_source = EXPLANATION_SOURCE_AUTO
    kp.explanation_model = model_name[:64]
    kp.explanation_updated_at = now
    kp.explanation_contributor_tasks = list(contributor_ids)
    db.flush()
    return True


def _sync_needed(kp: KnowledgePoint, contributor_ids: list[str], force: bool) -> bool:
    """幂等判定:贡献任务集合与已落库集合一致且讲解非空 → 跳过(否则动作)

    注:任务侧驱动对已有 auto 讲解也重写(新来源集 = 素材变了);manual
    在调用方已预先筛除,DB 层另有条件 UPDATE 兜底。
    """
    if force:
        return True
    if not (kp.explanation or "").strip():
        return True
    return {str(x) for x in (kp.explanation_contributor_tasks or [])} != set(contributor_ids)


def sync_knowledge_explanations(
    db: Session, user_id, knowledge_point_ids, *,
    client: LLMClient | None = None,
    custom_defs: list[dict] | None = None,
    force: bool = False,
    event_callback=None,
) -> dict:
    """任务侧驱动看板讲解:单任务采用 / 跨任务合并

    返回 {adopted, merged, skipped, no_source_keys}:
    no_source_keys 为规则 3 命中的知识点 key(无任务来源,由调用方决定
    是否走题干+发现重建的按需路径)。

    逐 KP 三条规则:
    1. 单贡献任务 → 直接采用该任务的 Result.content(零额外 LLM 调用);
    2. ≥2 贡献任务 → 调一次 LLM 合并为通用看板讲解(去掉具体文件路径行号);
    3. 无贡献任务(对话降级伪发现/老数据)→ 不在此处理,留给看板按需
       「生成讲解」(题干+发现重建),避免出题收尾白跑 LLM。
    覆盖规则:manual 永不覆盖;非 force 且贡献集合未变且已有讲解 → 跳过。
    调用方出题收尾必须包 try/except:讲解是增益,不得影响出题结果。
    custom_defs 为合并路径预留的主题词表参数(合并输入已是成品讲解,当前未用)。
    """
    stats = {"adopted": 0, "merged": 0, "skipped": 0, "no_source_keys": []}
    ids = [i for i in (knowledge_point_ids or []) if i]
    if not ids:
        return stats
    kps = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == user_id,
        KnowledgePoint.id.in_(ids),
    ).all()
    now = datetime.now(timezone.utc)
    merge_items: list[tuple[KnowledgePoint, list[dict]]] = []
    for kp in kps:
        if (kp.explanation_source or "") == EXPLANATION_SOURCE_MANUAL:
            stats["skipped"] += 1
            continue
        contributions = _task_contributions(db, kp)
        if not contributions:
            stats["skipped"] += 1
            stats["no_source_keys"].append(kp.key)  # 规则 3:按需路径负责
            continue
        contributor_ids = [c["task_id"] for c in contributions]
        if not _sync_needed(kp, contributor_ids, force):
            stats["skipped"] += 1
            continue
        if len(contributions) == 1:
            # 规则 1:采用不走模型,explanation_model 记 task_side
            if _write_explanation(
                db, kp, contributions[0]["explanation"], _ADOPTED_MODEL,
                contributor_ids, now,
            ):
                stats["adopted"] += 1
            else:
                stats["skipped"] += 1
            continue
        merge_items.append((kp, contributions))
    if merge_items:
        if client is None:
            logger.info(
                "[explain] user=%s %d 个知识点待跨任务合并,但讲解模型不可用,留待下次同步",
                user_id, len(merge_items),
            )
            stats["skipped"] += len(merge_items)
        else:
            stats["merged"] += _merge_batches(
                db, user_id, merge_items, client=client, event_callback=event_callback,
            )
    db.commit()
    logger.info(
        "[explain] user=%s 任务侧讲解同步: 采用 %d, 合并 %d, 跳过 %d",
        user_id, stats["adopted"], stats["merged"], stats["skipped"],
    )
    return stats


def _merge_batches(
    db: Session, user_id, merge_items: list[tuple[KnowledgePoint, list[dict]]], *,
    client: LLMClient,
    event_callback=None,
) -> int:
    """跨任务合并:一次调用覆盖 ≤MAX_KP_PER_BATCH 个知识点(口径同 explain)"""
    model_name = getattr(client, "model", "") or ""
    batches = [
        merge_items[i:i + MAX_KP_PER_BATCH]
        for i in range(0, len(merge_items), MAX_KP_PER_BATCH)
    ]
    _emit(event_callback, "start", total=len(merge_items), batches=len(batches),
          model=model_name)
    written = 0
    for batch in batches:
        try:
            written += _merge_one_batch(
                db, user_id, batch, client=client, model_name=model_name,
            )
        except Exception as e:
            # 单批失败不阻断其它批次,更不牵连出题主流程
            logger.warning(
                "[explain] user=%s 合并批次失败(keys=%s): %s",
                user_id, [kp.key for kp, _ in batch][:3], e,
            )
    _emit(event_callback, "done", written=written, total=len(merge_items))
    return written


def _merge_one_batch(
    db: Session, user_id, batch: list[tuple[KnowledgePoint, list[dict]]], *,
    client: LLMClient, model_name: str,
) -> int:
    """一批跨任务知识点的合并:拼提示词 → 调模型 → 解析 → 落库(不 commit)"""
    items = [
        {
            "knowledge_key": kp.key,
            "knowledge_name": kp.name,
            "entries": [
                {
                    "task_title": c["task_title"],
                    "explanation": _clip(c["explanation"], _FINDING_CONTENT_CHARS),
                }
                for c in contributions
            ],
        }
        for kp, contributions in batch
    ]
    messages = [
        {"role": "system", "content": build_merge_explain_system_prompt()},
        {"role": "user", "content": build_merge_explain_user_prompt(items)},
    ]
    content = _collect_llm_text(client, messages)
    parsed = _parse_explanations(content)
    if not parsed:
        logger.warning(
            "[explain] user=%s 合并批次无有效输出,样例: %r",
            user_id, (content or "")[:200],
        )
        return 0
    written = 0
    for kp, contributions in batch:
        markdown = parsed.get(kp.key)
        if not markdown:
            continue
        if _write_explanation(
            db, kp, markdown, model_name,
            [c["task_id"] for c in contributions], datetime.now(timezone.utc),
        ):
            written += 1
    db.flush()
    return written


def _emit(event_callback, phase: str, **extra) -> None:
    """推 explain 阶段事件;回调异常不影响讲解主流程"""
    if event_callback is None:
        return
    try:
        event_callback("explain", {"phase": phase, **extra})
    except Exception:
        logger.warning("[explain] 事件回调异常(忽略)", exc_info=True)
