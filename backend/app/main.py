"""FastAPI 应用入口"""
import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI

from app.config import settings
from app.database import Base, engine
from app.log_redaction import install_log_redaction
from app.routers import agent_configs, auth, health, skills, tasks
from app.routers import git_provider as git_provider_router
from app.routers import model_configs as model_configs_router
from app.routers import uploads as uploads_router
from app.routers import workspace as workspace_router
from app.routers import memory as memory_router

# 日志配置:开发期 DEBUG,生产期 INFO
# 通过 LOG_LEVEL 环境变量覆盖(默认按 APP_ENV 决定)
_log_level = getattr(settings, "LOG_LEVEL", None) or (
    "DEBUG" if settings.APP_DEBUG else "INFO"
)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# 凭证脱敏:Gitee 把 access_token 放在 URL query 上,httpx 会把完整 URL 打进
# INFO 日志(生产默认级别),长期有效的 token 因此落到日志文件/采集链路里。
# 统一在 handler 上过滤,详见 app/log_redaction.py
install_log_redaction()

# 出题链路专用滚动日志:logs/practice_generate.log(与 perf.log 同目录约定)
# 排查“一道题也没生成”需要持久化记录:模型解析/工作区状态/每条 finding 的
# 解析与丢弃原因/汇总结果;控制台输出保留(propagate 不关)
_PRACTICE_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "practice_generate.log"
try:
    _PRACTICE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _practice_handler = RotatingFileHandler(
        _PRACTICE_LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _practice_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    # 挂到出题相关 logger:services.practice(generator/auto_generate/jobs)
    # 与 routers.practice(job 线程);文件 handler 随 INFO 级别全量落盘
    _practice_handler.setLevel(logging.INFO)
    install_log_redaction(_practice_handler)
    for _name in ("app.services.practice", "app.routers.practice"):
        logging.getLogger(_name).addHandler(_practice_handler)
except Exception:  # 日志落盘失败不影响应用启动
    logging.getLogger(__name__).warning(
        "出题日志文件初始化失败: %s", _PRACTICE_LOG_FILE, exc_info=True
    )

# 导入场景模块,触发注册(general 放首位 → 前端新建任务默认选中"通用")
from app.scenarios import general  # noqa: F401
from app.scenarios import code_review  # noqa: F401
from app.scenarios import document_review  # noqa: F401

# 阶段 5:启动时扫描所有 SKILL.md,加载到进程级注册表
from app.skills.loader import reload_registry
reload_registry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期:启动时建表

    默认仅 create_all(幂等,不会重建已存在的表)。
    需要 schema 变更时,在 .env 设置 DB_REBUILD_ON_START=true 触发 drop_all,
    生产环境应切换到 Alembic 迁移管理 schema 变更。
    """
    # 一次性目录迁移:旧默认数据目录(_repos/_repo_cache/user_skills/uploads_data)
    # → data/ 统一根。必须先于一切目录消费方(GC/工作区恢复/路由初始化)执行;
    # 设置被 env 重定位时不动,失败不阻断启动(详见 app/services/data_dirs.py)
    from app.services.data_dirs import migrate_legacy_data_dirs

    migrate_legacy_data_dirs()

    from app.models import email_token, task, user  # noqa: F401
    from app.models import agent_policy  # noqa: F401  # 用户级智能体策略独立表
    from app.models import task_artifact  # noqa: F401
    from app.models import user_agent_config  # noqa: F401
    from app.models import user_git_binding  # noqa: F401
    from app.models import user_llm_config  # noqa: F401
    from app.models import project  # noqa: F401
    from app.models import practice  # noqa: F401  # 练习模块全新表,随 create_all 建表
    from app.models import domain_event_log  # noqa: F401  # 领域事件审计日志,随 create_all 建表
    from app.models import memory_settings  # noqa: F401  # 记忆生成设置 1:1 表,随 create_all 建表
    # 审查项(证据驱动可信审查):review_items 表须晚于 practice_questions 的
    # source_review_item_id 外键迁移(create_all 建缺失表,须先注册 audit)
    from app.models import audit  # noqa: F401

    if settings.DB_REBUILD_ON_START:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # 一次性迁移:把旧 users.github_id/github_access_token 搬到 user_git_bindings
    from app.models.user_git_binding import (
        add_login_avatar_columns,
        add_refresh_token_columns,
        migrate_legacy_github_bindings,
    )

    migrate_legacy_github_bindings()
    # 加 refresh_token / expires_at 列(Gitee access_token 刷新机制)
    add_refresh_token_columns()
    # 加 provider_login / avatar_url 列(status 接口缓存 login+avatar,避免每次调 /user)
    add_login_avatar_columns()
    # 加 projects.memory_summary 列(精简版记忆,注入 system prompt 用)
    from app.models.project import migrate_project_memory_summary

    migrate_project_memory_summary()
    # 补 memory_settings 的结构化类别列(project_categories/global_categories,幂等)
    from app.models.memory_settings import migrate_memory_add_category_columns

    migrate_memory_add_category_columns()
    # 迁移 user_preferences:删遗留 preferences 列,custom_prompt 改名 user_profile
    from app.models.user_preference import migrate_user_preference_columns

    migrate_user_preference_columns()
    # 迁移练习设置:user_preferences.auto_generate_practice → practice_settings 独立表
    # (拷数据后删旧列;新表已由 create_all 建好)
    from app.models.practice import migrate_practice_settings_table

    migrate_practice_settings_table()
    # 补练习域新列:practice_settings.learning_topic / restore_workspace_for_practice
    # + practice_questions.learning_topic(幂等,全新库直接返回)
    from app.models.practice import migrate_practice_learning_columns

    migrate_practice_learning_columns()
    # 迁移用户级 agent_policy:user_preferences.agent_policy JSONB → agent_policies 独立表
    # (拷数据后删旧列;必须晚于 migrate_user_preference_columns,新表已由 create_all 建好)
    from app.models.agent_policy import (
        migrate_agent_policy_add_reference_check_column,
        migrate_agent_policy_rename_columns,
        migrate_agent_policy_table,
    )

    # 补 allow_reference_check 列(引用复核开关):必须先于
    # migrate_agent_policy_table(其 INSERT 引用该列)
    migrate_agent_policy_add_reference_check_column()
    migrate_agent_policy_table()
    # 重命名 agent_policies 旧列 user_agent_enabled → agent2_enabled(修复智能体策略页 500)
    migrate_agent_policy_rename_columns()
    # 检查点/打断功能移除:删 agent_policies 5 个旧列 + 清理 conversations 历史过程记录
    from app.models.agent_policy import (
        migrate_agent_policy_drop_checkpoint_columns,
        migrate_agent_policy_drop_max_rounds_column,
        migrate_conversations_drop_checkpoint_records,
    )

    migrate_agent_policy_drop_checkpoint_columns()
    migrate_conversations_drop_checkpoint_records()
    # 协作总轮次设置移除:删 agent_policies.max_rounds 旧列(后台审查后初始运行单轮,
    # 多轮由用户 resume 驱动;须晚于 migrate_agent_policy_table,其 INSERT 已不写该列)
    migrate_agent_policy_drop_max_rounds_column()
    # 加 conversations.tool_call_id 列(tool_result 关联对应 tool_call,并行调用时前端精确配对)
    from app.models.task import (
        migrate_conversation_add_attachments_column,
        migrate_conversation_tool_call_id,
        migrate_stale_review_status,
        migrate_task_add_review_status_column,
        migrate_task_drop_checklist_column,
    )

    migrate_conversation_tool_call_id()
    # 追问多文件上传:加 conversations.attachments 列(附件展示信息,刷新后气泡仍渲染 chip)
    migrate_conversation_add_attachments_column()
    # 覆盖度清单功能移除:删 tasks.checklist 旧列(幂等)
    migrate_task_drop_checklist_column()
    # agent2 后台审查:加 tasks.review_status 列 + 清理遗留 running 状态
    # (后端重启后审查线程已死,启动时置 failed,避免前端永远"检查中")
    migrate_task_add_review_status_column()
    migrate_stale_review_status()

    # 领域事件:注册审计订阅者(所有事件 append-only 落库 domain_event_logs;
    # 建表已完成,后续扩展按同样方式 subscribe,见 app/domain_events.py)
    from app.services.domain_event_audit import register_audit_subscriber

    _unsub_audit = register_audit_subscriber()

    # 交付物保留/GC 后台协程:周期清理终态超期 / 孤儿上传
    # (单 worker 部署无多进程重复执行风险;关闭时 cancel)
    _gc_task = None
    if settings.UPLOAD_GC_ENABLED:
        from app.services.upload_gc import gc_loop

        _gc_task = asyncio.create_task(gc_loop())

    # 孤儿临时目录恢复(local 模式):进程重启后 _sessions 内存丢失,
    # mkdtemp 目录残留磁盘。后台线程抢救未保存的 diff 后清理(防泄漏);
    # 不阻塞启动(rmtree 大目录可能秒级),无需 cancel(daemon 线程)
    if settings.SANDBOX_MODE == "local":
        import threading

        from app.services.workspace_diff import recover_orphan_local_workspaces

        threading.Thread(
            target=recover_orphan_local_workspaces,
            name="orphan-workspace-recovery",
            daemon=True,
        ).start()

    yield

    if _gc_task is not None:
        _gc_task.cancel()  # 优雅关闭:停止 GC 协程
    _unsub_audit()  # 优雅关闭:退订审计订阅


app = FastAPI(
    title="SecondLook",
    description="双智能体协作系统",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(tasks.router)
app.include_router(skills.router)
app.include_router(auth.router)
app.include_router(model_configs_router.router)
app.include_router(git_provider_router.router)
app.include_router(workspace_router.router)
app.include_router(agent_configs.router)
app.include_router(memory_router.router)
# 任务交付物上传(ZIP / 单文件):上传与任务创建解耦,提交任务时引用 upload_id
app.include_router(uploads_router.router)
# 出题 & 练习功能总开关:关闭时 /practice/* 全部 404(路由不注册)
if settings.PRACTICE_ENABLED:
    from app.routers import learning_topics as learning_topics_router
    from app.routers import practice as practice_router

    app.include_router(practice_router.router)
    # 学习主题词表 CRUD(内置 4 个 + 用户自定义,分类与出题视角的定义源)
    app.include_router(learning_topics_router.router)


@app.get("/")
def root() -> dict:
    return {"name": "SecondLook", "version": "0.2.0", "docs": "/docs"}
