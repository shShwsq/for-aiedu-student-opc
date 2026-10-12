"""review_items 列改名与旧"计划回填"条目清理的幂等迁移测试

无 Alembic:项目用 Base.metadata.create_all,已存在的表不会自动改名/加列,
故迁移函数须能在"老库/新库/已迁过"三种形态下都安全执行(main.py lifespan 每次启动都会跑)。
按仓库既有做法 monkeypatch app.database.engine 与 sqlalchemy.inspect,断言 SQL 形状。
"""
from unittest.mock import MagicMock

import app.database as database_module  # noqa: F401  迁移函数内 from-import 在调用时取属性
from app.models.audit import (
    migrate_review_item_columns,
    purge_legacy_plan_backfill_items,
)


def _migration_env(monkeypatch, has_table=True, columns=None):
    """构造迁移的假环境(同 test_agent_policy._migration_env),返回 mock conn"""
    conn = MagicMock()
    inspector = MagicMock()
    inspector.has_table.return_value = has_table
    inspector.get_columns.return_value = [{"name": c} for c in (columns or [])]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr("sqlalchemy.inspect", lambda c: inspector)
    return conn


def _sqls(conn) -> list[str]:
    return [str(c.args[0]) for c in conn.execute.call_args_list]


# ============================================================
# 列改名 + 补 plan_item_id
# ============================================================

def test_rename_migration_renames_both_columns_and_adds_plan_item_id(monkeypatch):
    conn = _migration_env(monkeypatch, columns=[
        "id", "task_id", "round_idx", "title", "description",
        "review_target", "origin", "status", "verdict", "suggestion",
    ])
    migrate_review_item_columns()

    sqls = _sqls(conn)
    assert any(
        "ALTER TABLE review_items RENAME COLUMN description TO conclusion" in s for s in sqls
    )
    assert any(
        "ALTER TABLE review_items RENAME COLUMN suggestion TO follow_up" in s for s in sqls
    )
    assert any("ADD COLUMN plan_item_id VARCHAR(16)" in s for s in sqls)
    conn.commit.assert_called_once()


def test_rename_migration_is_idempotent_on_new_shape(monkeypatch):
    """新库形状(conclusion/follow_up/plan_item_id 齐)→ 一条 ALTER 都不发"""
    conn = _migration_env(monkeypatch, columns=[
        "id", "title", "conclusion", "review_target", "origin",
        "plan_item_id", "status", "verdict", "follow_up",
    ])
    migrate_review_item_columns()

    conn.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_rename_migration_adds_only_missing_plan_item_id(monkeypatch):
    """已改过名但缺 plan_item_id(改名先上线、补列后上线的中间态)→ 只补列"""
    conn = _migration_env(monkeypatch, columns=[
        "id", "title", "conclusion", "review_target", "origin", "status", "follow_up",
    ])
    migrate_review_item_columns()

    sqls = _sqls(conn)
    assert len(sqls) == 1
    assert "ADD COLUMN plan_item_id" in sqls[0]


def test_rename_migration_skips_when_table_missing(monkeypatch):
    """全新库:create_all 直接建新形状,迁移不参与(误发 ALTER 会炸启动)"""
    conn = _migration_env(monkeypatch, has_table=False)
    migrate_review_item_columns()

    conn.execute.assert_not_called()
    conn.commit.assert_not_called()


# ============================================================
# 清理旧版规划对账伪造的"[计划回填]"条目
# ============================================================

def test_purge_deletes_only_legacy_backfill_rows(monkeypatch):
    conn = _migration_env(monkeypatch, columns=[
        "id", "title", "conclusion", "review_target", "status", "verdict", "follow_up",
    ])
    purge_legacy_plan_backfill_items()

    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "DELETE FROM review_items" in sql
    # 三个条件同时命中才删:标题前缀 + 未执行状态 + 旧占位正文(不碰真实审查项)
    assert "title LIKE :prefix" in sql
    assert "status = 'missing'" in sql
    assert "conclusion = :placeholder" in sql
    assert params["prefix"] == "[计划回填]%"
    conn.commit.assert_called_once()


def test_purge_uses_description_when_rename_has_not_run(monkeypatch):
    """迁移顺序被打断(列仍叫 description)时按现有列名删,不抛异常"""
    conn = _migration_env(monkeypatch, columns=[
        "id", "title", "description", "review_target", "status", "suggestion",
    ])
    purge_legacy_plan_backfill_items()

    sql = str(conn.execute.call_args.args[0])
    assert "description = :placeholder" in sql


def test_purge_skips_when_table_or_column_missing(monkeypatch):
    conn = _migration_env(monkeypatch, has_table=False)
    purge_legacy_plan_backfill_items()
    conn.execute.assert_not_called()

    conn2 = _migration_env(monkeypatch, columns=["id", "title", "status"])
    purge_legacy_plan_backfill_items()
    conn2.execute.assert_not_called()
