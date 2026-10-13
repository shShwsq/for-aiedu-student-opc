"""数据库连接与会话管理"""
import logging
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

# 连接池配置:阿里云 PostgreSQL 推荐配置
# connect_args.connect_timeout:握手层硬超时(秒),库不可达时快速失败而非阻塞到
# OS TCP 超时(60–130s)——那会把 pytest / 服务启动拖成"卡死无输出"。健康库远低于此。
# connect_args keepalives:让空闲连接在 TCP 层周期性探活,降低被 RDS 读写分离
# 代理(rwlb)按 idle 超时主动关闭的概率。注:pool_pre_ping / pool_recycle 只在
# 从池 checkout 时生效,对"已被 session 借出、长时间持有"的连接无效 —— 那种场景
# 靠上层"阻塞前释放连接 / 按轮重建会话"纪律兜底(见 pause_controller、orchestrator)。
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # 连接前检查是否有效,避免阿里云 RDS 主动断连后报错
    pool_size=5,  # 连接池大小
    max_overflow=10,  # 超出 pool_size 后还能创建的连接数
    pool_recycle=600,  # 连接回收时间(秒);取低于 RDS 代理 idle 阈值的经验值,缩短坏连接驻池
    connect_args={
        "connect_timeout": max(1, int(settings.DB_CONNECT_TIMEOUT_SECONDS or 5)),
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    },
    echo=settings.APP_DEBUG,  # 开发环境打印 SQL
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""

    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入:每个请求获取独立数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def persist_task_failure_fresh(
    task_id, error_detail: str, stage: str,
) -> bool:
    """用全新独立会话把任务落为 FAILED,主连接已死时的终态兜底。

    背景:流水线崩溃往往正是主 db session 的连接被服务端掐断所致,此时复用同一条
    session 写 FAILED 会二次抛错、被 except 静默吞掉,导致库里状态永远停在 RUNNING
    (前端重连 SSE 读库拿到 running 就永久转圈)。本函数刻意不碰调用方的 session:
    新开 SessionLocal、失败时 dispose 丢弃坏连接再重试一次,全程吞异常不外抛,只回
    返回"是否成功落库"。事件推送(走内存总线)由调用方负责,不依赖本函数成功。

    返回 True 表示已落 FAILED(或任务已是终态,视为无需再落);False 表示兜底也失败。
    """
    # 延迟导入避免与 models(顶层从本模块导入 Base)形成循环依赖
    from uuid import UUID

    from app.models.task import Task, TaskStatus

    task_uuid = task_id if isinstance(task_id, UUID) else UUID(str(task_id))
    last_exc: Exception | None = None
    for attempt in range(2):
        if attempt:
            # 上一轮疑似撞上被服务端关闭的坏连接:丢弃池中连接,下次拿全新的
            try:
                engine.dispose()
            except Exception:  # noqa: BLE001 - dispose 失败不应阻断重试
                pass
        db = SessionLocal()
        try:
            task = db.get(Task, task_uuid)
            if task is None:
                return False
            # 已是终态(正常完成/已失败)不覆盖,视为无需再落
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                return True
            task.status = TaskStatus.FAILED
            task.error_message = (error_detail or "")[:1000]
            task.current_stage = stage
            db.commit()
            return True
        except SQLAlchemyError as e:
            db.rollback()
            last_exc = e
        except Exception as e:  # noqa: BLE001 - 兜底路径任何异常都吞掉,留待重试/返回 False
            db.rollback()
            last_exc = e
        finally:
            db.close()
    logger.error(
        f"[task={task_uuid}] persist_task_failure_fresh 兜底落 FAILED 失败(已重试): {last_exc}"
    )
    return False
