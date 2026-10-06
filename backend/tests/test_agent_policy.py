"""app/agent_policy.py + agent2.build_tool_window_section 单元测试

覆盖:
- DEFAULT_AGENT_POLICY:常量字段完整性
- resolve_agent_policy:用户级默认 + 任务级覆盖合并优先级
- build_tool_window_section(app.agents.agent2):完整评估的工具调用窗口构造
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.agent_policy import DEFAULT_AGENT_POLICY, resolve_agent_policy
from app.agents.agent2 import build_tool_window_section


# ============================================================
# 测试 fixture
# ============================================================

@pytest.fixture
def fake_task():
    """构造 fake task:user_id=None,params=None(匿名任务,只用默认值)。"""
    task = MagicMock()
    task.user_id = None
    task.params = None
    task.scenario = "general"
    task.id = "test-task-id"
    task.user_input = "测试用户输入"
    return task


def _make_task_with_overrides(user_id=None, params=None, scenario="general"):
    """构造 fake task,指定 user_id / params / scenario。"""
    task = MagicMock()
    task.user_id = user_id
    task.params = params
    task.scenario = scenario
    task.id = "test-task-id"
    task.user_input = "测试用户输入"
    return task


def _mock_db_with_policy(policy_row=None):
    """构造 mock db:db.query(AgentPolicy).filter(...).first() 返回 policy_row。

    policy_row=None 表示用户未保存过智能体策略(查无记录)。
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = policy_row
    return db


# ============================================================
# DEFAULT_AGENT_POLICY 常量
# ============================================================

def test_default_policy_has_all_required_fields():
    """DEFAULT_AGENT_POLICY 应包含所有必需字段,默认值符合设计。"""
    expected_keys = {
        "agent2_enabled",
        "allow_verify",
        "allow_reference_check",
        "verifier_auth_mode_default",
        "executor_command_confirm_default",
    }
    assert set(DEFAULT_AGENT_POLICY.keys()) == expected_keys
    # 关键默认值(与设计文档 / 前端 DEFAULT_POLICY 对齐)
    assert DEFAULT_AGENT_POLICY["agent2_enabled"] is True
    assert DEFAULT_AGENT_POLICY["allow_verify"] is False
    assert DEFAULT_AGENT_POLICY["allow_reference_check"] is True
    assert DEFAULT_AGENT_POLICY["verifier_auth_mode_default"] == "per_action"
    assert DEFAULT_AGENT_POLICY["executor_command_confirm_default"] == "always_approve"
    # max_rounds(协作总轮次)已随后台审查重构移除:初始运行单轮,多轮由用户 resume 驱动
    assert "max_rounds" not in DEFAULT_AGENT_POLICY


# ============================================================
# resolve_agent_policy:优先级 DEFAULT > 场景默认 > user > task
# ============================================================


def test_resolve_returns_defaults_for_anonymous_task(fake_task):
    """匿名任务(user_id=None)+ 无 params → 纯默认值。"""
    db = _mock_db_with_policy(policy_row=None)
    policy = resolve_agent_policy(fake_task, db)

    # 应等于默认值(无 max_rounds 键:协作总轮次已移除)
    assert "max_rounds" not in policy
    assert policy["allow_verify"] is False
    assert policy["allow_reference_check"] is True
    assert policy["verifier_auth_mode_default"] == "per_action"


def test_resolve_uses_user_level_defaults_when_no_overrides():
    """有用户级默认 + 无任务级覆盖 → 用用户级默认。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    policy_row = MagicMock()
    policy_row.to_dict.return_value = {
        "allow_verify": True,
        "allow_reference_check": False,
    }
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is True
    assert policy["allow_reference_check"] is False
    # 未覆盖的字段仍用 DEFAULT
    assert policy["verifier_auth_mode_default"] == "per_action"


def test_resolve_task_overrides_win_over_user_defaults():
    """任务级覆盖应优先于用户级默认。"""
    task = _make_task_with_overrides(
        user_id=uuid.uuid4(),
        params={"_agent_policy": {"allow_verify": False, "allow_reference_check": True}},
    )
    policy_row = MagicMock()
    policy_row.to_dict.return_value = {
        "allow_verify": True,  # 应被任务级 False 覆盖
        "allow_reference_check": False,  # 应被任务级 True 覆盖
        "verifier_auth_mode_default": "direct",  # 任务级未覆盖,应保留用户级
    }
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is False          # 任务级覆盖
    assert policy["allow_reference_check"] is True  # 任务级覆盖
    assert policy["verifier_auth_mode_default"] == "direct"  # 用户级保留


def test_resolve_task_overrides_win_over_defaults_for_anonymous():
    """匿名任务 + 任务级覆盖 → 任务级覆盖 DEFAULT。"""
    task = _make_task_with_overrides(
        user_id=None,
        params={"_agent_policy": {"allow_verify": True}},
    )
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is True
    assert policy["allow_reference_check"] is True  # 未覆盖,用默认


def test_resolve_handles_saved_default_policy():
    """用户保存过策略但值等于系统默认(to_dict 返回 DEFAULT)→ 结果仍为 DEFAULT。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    policy_row = MagicMock()
    policy_row.to_dict.return_value = dict(DEFAULT_AGENT_POLICY)
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is False


def test_resolve_handles_no_policy_row():
    """用户无 AgentPolicy 行(first() 返回 None)→ 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["agent2_enabled"] is True


def test_resolve_handles_empty_params_dict():
    """task.params = {} (空字典,非 None) → 无任务级覆盖,用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=None, params={})
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["agent2_enabled"] is True


def test_resolve_handles_empty_agent_policy_in_params():
    """task.params = {"_agent_policy": {}} (空覆盖) → 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=None, params={"_agent_policy": {}})
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["agent2_enabled"] is True


def test_resolve_ignores_legacy_max_rounds_override():
    """老任务 params 里残留 max_rounds → 忽略该键(不进结果,不影响其他覆盖)。"""
    task = _make_task_with_overrides(
        user_id=None,
        params={"_agent_policy": {"max_rounds": 9, "allow_verify": True}},
    )
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert "max_rounds" not in policy
    assert policy["allow_verify"] is True  # 同级其他覆盖正常生效


# ============================================================
# 场景默认:安全类场景自动 allow_verify(用户/任务级显式值优先)
# ============================================================


def test_security_scenario_auto_enables_allow_verify():
    """code_security_audit 场景 + 从未显式设置 → allow_verify 自动开。"""
    task = _make_task_with_overrides(
        user_id=uuid.uuid4(), params=None, scenario="code_security_audit"
    )
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is True
    # 其他字段不受影响
    assert policy["allow_reference_check"] is True


def test_non_security_scenario_keeps_allow_verify_off():
    """非安全场景(general)→ allow_verify 保持默认关。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None,
                                     scenario="general")
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is False


def test_security_scenario_user_saved_policy_wins():
    """用户显式保存过策略(allow_verify=False)→ 优先于场景默认。"""
    task = _make_task_with_overrides(
        user_id=uuid.uuid4(), params=None, scenario="code_security_audit"
    )
    policy_row = MagicMock()
    policy_row.to_dict.return_value = {
        "agent2_enabled": True, "allow_verify": False,
        "allow_reference_check": True,
        "verifier_auth_mode_default": "per_action",
        "executor_command_confirm_default": "always_approve",
    }
    db = _mock_db_with_policy(policy_row=policy_row)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is False


def test_security_scenario_task_override_wins():
    """任务级显式设置 allow_verify → 优先于场景默认。"""
    task = _make_task_with_overrides(
        user_id=None,
        params={"_agent_policy": {"allow_verify": False}},
        scenario="code_security_audit",
    )
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_verify"] is False


def test_task_level_can_disable_reference_check():
    """任务级可显式关闭 allow_reference_check。"""
    task = _make_task_with_overrides(
        user_id=None,
        params={"_agent_policy": {"allow_reference_check": False}},
    )
    db = _mock_db_with_policy(policy_row=None)

    policy = resolve_agent_policy(task, db)
    assert policy["allow_reference_check"] is False


# ============================================================
# build_tool_window_section:工具调用窗口(agent2 完整评估注入)
# ============================================================

def _make_tool_conv(type_: str, content: str, created_at=None):
    """构造 fake react_agent tool_call/tool_result Conversation。"""
    conv = MagicMock()
    conv.type = type_
    conv.content = content
    conv.created_at = created_at
    return conv


def _mock_tool_window_db(convs):
    """mock db:无 boundary 时 db.query().filter().order_by().all() 返回 convs。"""
    db = MagicMock()
    q1 = db.query.return_value.filter.return_value
    q1.order_by.return_value.all.return_value = convs
    return db


def test_tool_window_formats_intent_and_truncates_result():
    """tool_call 只取首行意图(参数 JSON 被剥离),tool_result 截断。"""
    convs = [
        _make_tool_conv("tool_call", "搜索 SQL 注入相关代码\n{\"pattern\": \"execute\"}"),
        _make_tool_conv("tool_result", "匹配结果" + "x" * 500),
    ]
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]")
    assert section.startswith("[窗口]")
    assert "- 搜索 SQL 注入相关代码" in section
    assert '"pattern"' not in section  # 参数详情被剥离
    assert "结果摘要:" in section
    assert "[...truncated...]" in section


def test_tool_window_empty_when_no_convs():
    """窗口内无任何工具调用 → 返回空串。"""
    db = _mock_tool_window_db([])
    assert build_tool_window_section(db, "t", 1, None, title="[窗口]") == ""


def test_tool_window_drops_earliest_when_max_calls_exceeded():
    """tool_call 条数超 max_calls 时从最早丢弃,并清理孤立结果。"""
    convs = []
    for i in range(1, 5):  # 4 个 tool_call,各带结果
        convs.append(_make_tool_conv("tool_call", f"意图{i}"))
        convs.append(_make_tool_conv("tool_result", f"结果{i}"))
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]", max_calls=2)
    assert "意图1" not in section
    assert "意图2" not in section
    assert "结果2" not in section  # 对应 call 被丢,孤立结果一并清理
    assert "意图3" in section
    assert "意图4" in section


def test_tool_window_drops_earliest_when_max_chars_exceeded():
    """总长超 max_chars 时从最早丢弃。"""
    convs = [
        _make_tool_conv("tool_call", "早期意图" + "a" * 300),
        _make_tool_conv("tool_call", "近期意图" + "b" * 300),
    ]
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]", max_chars=400)
    assert "早期意图" not in section
    assert "近期意图" in section


def test_tool_window_skips_empty_content():
    """content 为空的记录跳过,不产生空行。"""
    convs = [
        _make_tool_conv("tool_call", ""),
        _make_tool_conv("tool_call", "有效意图"),
    ]
    db = _mock_tool_window_db(convs)
    section = build_tool_window_section(db, "t", 1, None, title="[窗口]")
    assert "有效意图" in section
    assert section.count("\n- ") == 1


# ============================================================
# 迁移:agent_policies.allow_reference_check 幂等补列
# ============================================================


def _migration_env(monkeypatch, has_table=True, columns=None):
    """构造 migrate_agent_policy_add_reference_check_column 的假环境。

    monkeypatch app.database.engine(函数内 from-import 在调用时取属性)
    与 sqlalchemy.inspect;返回 mock conn 供断言。
    """
    import app.database as database_module

    conn = MagicMock()
    inspector = MagicMock()
    inspector.has_table.return_value = has_table
    inspector.get_columns.return_value = [{"name": c} for c in (columns or [])]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr("sqlalchemy.inspect", lambda c: inspector)
    return conn


def test_migration_adds_column_when_missing(monkeypatch):
    """表存在但缺 allow_reference_check 列 → ALTER 补列并提交。"""
    conn = _migration_env(
        monkeypatch, columns=["id", "user_id", "agent2_enabled", "max_rounds"],
    )
    from app.models.agent_policy import (
        migrate_agent_policy_add_reference_check_column,
    )

    migrate_agent_policy_add_reference_check_column()

    conn.execute.assert_called_once()
    sql = str(conn.execute.call_args.args[0])
    assert "allow_reference_check" in sql
    conn.commit.assert_called_once()


def test_migration_skips_when_column_exists(monkeypatch):
    """列已存在 → 幂等跳过,不执行 ALTER 不提交。"""
    conn = _migration_env(
        monkeypatch, columns=["id", "agent2_enabled", "allow_reference_check"],
    )
    from app.models.agent_policy import (
        migrate_agent_policy_add_reference_check_column,
    )

    migrate_agent_policy_add_reference_check_column()

    conn.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_migration_skips_when_table_missing(monkeypatch):
    """表不存在(全新库,create_all 已带新列)→ 直接返回。"""
    conn = _migration_env(monkeypatch, has_table=False)
    from app.models.agent_policy import (
        migrate_agent_policy_add_reference_check_column,
    )

    migrate_agent_policy_add_reference_check_column()

    conn.execute.assert_not_called()
    conn.commit.assert_not_called()


# ============================================================
# 迁移:agent_policies.max_rounds 旧列幂等删除(协作总轮次移除)
# ============================================================


def test_drop_max_rounds_migration_drops_when_exists(monkeypatch):
    """老库存在 max_rounds 列 → DROP 并提交。"""
    conn = _migration_env(
        monkeypatch, columns=["id", "user_id", "agent2_enabled", "max_rounds"],
    )
    from app.models.agent_policy import (
        migrate_agent_policy_drop_max_rounds_column,
    )

    migrate_agent_policy_drop_max_rounds_column()

    conn.execute.assert_called_once()
    sql = str(conn.execute.call_args.args[0])
    assert "DROP COLUMN max_rounds" in sql
    conn.commit.assert_called_once()


def test_drop_max_rounds_migration_skips_when_absent(monkeypatch):
    """全新库无 max_rounds 列 → 幂等跳过,不执行 DROP 不提交。"""
    conn = _migration_env(
        monkeypatch, columns=["id", "user_id", "agent2_enabled"],
    )
    from app.models.agent_policy import (
        migrate_agent_policy_drop_max_rounds_column,
    )

    migrate_agent_policy_drop_max_rounds_column()

    conn.execute.assert_not_called()
    conn.commit.assert_not_called()


def test_drop_max_rounds_migration_skips_when_table_missing(monkeypatch):
    """表不存在 → 直接返回。"""
    conn = _migration_env(monkeypatch, has_table=False)
    from app.models.agent_policy import (
        migrate_agent_policy_drop_max_rounds_column,
    )

    migrate_agent_policy_drop_max_rounds_column()

    conn.execute.assert_not_called()
    conn.commit.assert_not_called()
