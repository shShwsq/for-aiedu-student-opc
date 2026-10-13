"""测试基建:集中式"数据库不可达即跳过"守卫。

背景:多数 DB 测试连的是远端 PostgreSQL(见 backend/.env 的 DATABASE_URL)。库
不可达时,若沿用**没有 connect_timeout** 的连接,psycopg 会阻塞到 OS 层 TCP
超时(常见 60–130s),整轮 pytest 看起来"卡死无输出"。此前这套"快速失败 + 跳过"
逻辑是逐份复制进各 schema 测试模块的 session fixture,容易漏、且不覆盖走 app
全局引擎的用例。

本 conftest 把它收敛成单一事实来源:
- 会话内探测一次 DB 可达性(短 connect_timeout),缓存结果;
- 收集阶段给所有带 ``@pytest.mark.db`` 的用例统一打 skip(库不可达时);
- 探测仅在"本会话确实有 db 标记用例"时才发起,纯 mock 测试零额外开销。

配合 app/database.py 的 connect_timeout(生产/测试都快速失败),即便漏打标记的
直连引擎用例也只是约 5s 报错,不再拖死整轮。
"""
from __future__ import annotations

import pytest

from app.config import settings

# 统一注册 ORM mapper,消除收集顺序引起的"配置半截"崩溃:某些模块只 import 了带
# 双向关系的一侧(如 app.models.user_git_binding),没 import 另一侧(app.models.user),
# 一旦有用例触发 configure_mappers,User.git_bindings / binding.user 解析不到对方 →
# 全局 mapper 初始化失败,波及同轮其它用例(典型:plan_parallel + emit_split 并跑)。
# conftest 早于所有测试模块导入,这里补齐那对反向引用,任何顺序下 registry 都完整。
import app.models.user  # noqa: E402,F401
import app.models.user_git_binding  # noqa: E402,F401


def _db_reachable() -> bool:
    """一次性探测:能连上 DATABASE_URL 返回 True,连不上/超时返回 False。

    用独立短连接 + connect_timeout(而非复用 app 全局引擎):不可达时快速失败,
    并把异常一律收敛成 False,绝不让探测本身把收集阶段拖挂。
    """
    # 延迟导入:纯单元测试不该因导入本 conftest 就拉起 SQLAlchemy 引擎
    from sqlalchemy import create_engine, text

    timeout = max(1, int(getattr(settings, "DB_CONNECT_TIMEOUT_SECONDS", 5) or 5))
    try:
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"connect_timeout": timeout},
        )
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - 探测失败一律视为不可达,交由 skip 处理
        return False


def pytest_collection_modifyitems(session, config, items):
    """库不可达时给所有 @pytest.mark.db 用例打 skip(会话内只探测一次)。"""
    db_items = [it for it in items if it.get_closest_marker("db")]
    if not db_items:
        return  # 本会话无 DB 用例:不探测,纯 mock 测试零开销
    if _db_reachable():
        return

    # 不打印 DATABASE_URL(含账号口令),只给通用原因 + 指路 .env
    skip_mark = pytest.mark.skip(
        reason="数据库不可达或连接超时,跳过需要真实 DB 的用例(见 backend/.env DATABASE_URL)"
    )
    for it in db_items:
        it.add_marker(skip_mark)
