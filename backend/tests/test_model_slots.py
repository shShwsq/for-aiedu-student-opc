"""模型槽位(model slot)回归测试

覆盖「模型设置 > 任务模型默认」改造的关键不变量:
- 解析链:槽位默认 > env(含悬空 id、匿名、过渡期旧列兜底、脏值容忍)
- 注册表:槽位集合/编辑入口/唯一 writable 项(新增槽位时最容易漏的地方)
- 端点逻辑(会话替身,无需数据库):GET 聚合与 stale 标记、PUT 白名单/归属校验/
  部分写语义(None=不修改,空串=清空),失败不留半成品行
- 数据库集成(库不可达时跳过):迁移幂等与不覆盖、旧端点写穿、preferences 投影

DB 用例在配置的 PG 里建独立 schema(与 test_practice_api 同套路),
无建 schema 权限或库不可用时跳过。
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user

# 确保相关模型全部注册到 Base.metadata(create_all 才能建全表;
# TaskArtifact 的 relationship 按名字引用 Task,故 task 也要导入)
import app.models.memory_settings  # noqa: F401
import app.models.model_slot  # noqa: F401
import app.models.practice  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.user  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_llm_config  # noqa: F401

from app.models.memory_settings import MemorySettings
from app.models.model_slot import (
    SLOT_MEMORY_CURATOR,
    SLOT_PRACTICE_EXPLAIN,
    SLOT_PRACTICE_GENERATE,
    SLOT_REVIEW_DEFAULT,
    ModelSlotDefault,
    migrate_model_slot_defaults,
)
from app.models.practice import PracticeSettings
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.routers import memory as memory_router
from app.routers import model_configs as model_configs_router
from app.services import model_slots

CFG_A = {"id": "cfg-a", "name": "A", "provider": "dashscope", "model": "qwen-max"}
CFG_B = {"id": "cfg-b", "name": "B", "provider": "zhipu", "model": "glm-4"}


# ============================================================
# 替身:LLMClient / 数据库会话(端点逻辑不需要真库)
# ============================================================


class _FakeLLM:
    """伪造 LLMClient:env 构造与按配置构造可区分,避免真实网络/配置依赖"""

    def __init__(self, *a, **k):
        self.model = "env-default"
        self.enable_thinking = True

    @classmethod
    def from_config_dict(cls, cfg):
        inst = cls()
        inst.model = cfg.get("model") or "?"
        return inst


class _FakeQuery:
    """按查询模型 + filter 里的字面量匹配行(同一会话内多次查询结果稳定)"""

    def __init__(self, model, store):
        self.model = model
        self.store = store
        self._criteria = {}

    def filter(self, *criteria):
        for c in criteria:
            try:
                self._criteria[c.left.name] = c.right.value
            except AttributeError:
                continue  # 复合条件(本模块用不到)不参与字面量匹配
        return self

    def first(self):
        if self.model is ModelSlotDefault:
            # 一个 FakeSession 只代表一个用户的数据,故只按 slot 匹配
            # (用例里传的 user_id 是 "u1" 这种字面量,与行上的 UUID 无关)
            slot = self._criteria.get("slot")
            return next((r for r in self.store["slots"] if r.slot == slot), None)
        return self.store["by_model"].get(self.model)


class FakeSession:
    """够用的会话替身:槽位行 + 几张 1:1 设置行,支持 add/commit

    与真实库的已知差异(测试里不要依赖这些):
    - 列默认值/UUID 主键在 flush 时才生效,故 add() 里手工补 thinking_mode 默认
    - 不做唯一约束校验
    - flush() 只计数不真写库(用例只关心 set_slot 有没有在建行后刷一次)
    """

    def __init__(self, llm_configs=None, slots=None, settings_rows=None):
        self.store = {
            "slots": [],
            "by_model": {
                UserLLMConfig: SimpleNamespace(llm_configs=list(llm_configs or [])),
                **(settings_rows or {}),
            },
        }
        self.user_id = uuid.uuid4()
        for slot, values in (slots or {}).items():
            self.add(ModelSlotDefault(
                user_id=self.user_id, slot=slot,
                llm_config_id=values[0],
                thinking_mode=(values[1] if len(values) > 1 else "follow"),
            ))
        self.commits = 0
        self.flushes = 0

    def query(self, model):
        return _FakeQuery(model, self.store)

    def add(self, obj):
        if isinstance(obj, ModelSlotDefault):
            if obj.thinking_mode is None:
                obj.thinking_mode = "follow"  # 模拟 server_default
            self.store["slots"].append(obj)

    def commit(self):
        self.commits += 1

    def flush(self):
        self.flushes += 1

    def refresh(self, obj):  # pragma: no cover - 替身无需刷新
        return None

    def rollback(self):  # pragma: no cover
        return None

    def close(self):  # pragma: no cover
        return None

    def slot(self, slot_id):
        return next((r for r in self.store["slots"] if r.slot == slot_id), None)


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setattr(model_slots, "LLMClient", _FakeLLM)
    return _FakeLLM


# ============================================================
# 解析链(无 DB)
# ============================================================


def test_resolve_slot_hit(no_llm):
    db = FakeSession(llm_configs=[CFG_A], slots={SLOT_REVIEW_DEFAULT: ("cfg-a", "follow")})
    client, source = model_slots.resolve_slot_client(db, "u1", SLOT_REVIEW_DEFAULT)
    assert source == model_slots.SOURCE_SLOT
    assert client.model == "qwen-max"


def test_resolve_slot_stale_falls_back_env(no_llm):
    """槽位指向已被删除的配置:记日志回退 env,不抛异常"""
    db = FakeSession(llm_configs=[], slots={SLOT_REVIEW_DEFAULT: ("gone", "follow")})
    client, source = model_slots.resolve_slot_client(db, "u1", SLOT_REVIEW_DEFAULT)
    assert source == model_slots.SOURCE_ENV
    assert client.model == "env-default"


def test_resolve_no_row_uses_legacy_column(no_llm):
    """过渡期兜底:新表无行时读旧列值(迁移没跑时用户设置仍要生效)"""
    db = FakeSession(llm_configs=[CFG_B])
    client, source = model_slots.resolve_slot_client(
        db, "u1", SLOT_PRACTICE_GENERATE, legacy_value="cfg-b",
    )
    assert source == model_slots.SOURCE_SLOT
    assert client.model == "glm-4"


def test_row_without_id_still_falls_back_to_legacy_column(no_llm):
    """槽位行为 NULL(没做过模型决定,如只为思考模式建的行):配置 id 仍回退旧列;
    思考模式以新表为准。注意这与「已清空」不同 —— 见下一条。"""
    db = FakeSession(
        llm_configs=[CFG_B], slots={SLOT_MEMORY_CURATOR: (None, "on")},
    )
    assert model_slots.get_slot_config_id(
        db, "u1", SLOT_MEMORY_CURATOR, legacy_value="cfg-b",
    ) == "cfg-b"
    assert model_slots.get_slot_thinking_mode(
        db, "u1", SLOT_MEMORY_CURATOR, legacy_value="follow",
    ) == "on"


def test_cleared_slot_marker_wins_over_legacy_column(no_llm):
    """清空过的槽位(行上存 '')不再回退旧列,整条解析链落到 env 默认。

    初版缺陷:清空写成 NULL,读路径与启动迁移都把 NULL 当成「没设置」,
    结果存量用户点「跟随系统默认」后当场弹回、重启后又长回来。"""
    db = FakeSession(
        llm_configs=[CFG_B],
        slots={SLOT_MEMORY_CURATOR: (model_slots.CLEARED, "follow")},
    )
    assert model_slots.get_slot_config_id(
        db, "u1", SLOT_MEMORY_CURATOR, legacy_value="cfg-b",
    ) is None
    client, source = model_slots.resolve_slot_client(
        db, "u1", SLOT_MEMORY_CURATOR, legacy_value="cfg-b",
    )
    assert source == model_slots.SOURCE_ENV
    assert client.model == "env-default"
    # 聚合页也不能把旧列值当成生效值(否则「本次将使用」与实际不一致)
    view = {v["slot"]: v for v in model_slots.slot_views(db, "u1")}[SLOT_MEMORY_CURATOR]
    assert view["llm_config_id"] is None and view["source"] == "env"


def test_resolve_anonymous_uses_env(no_llm):
    db = FakeSession(llm_configs=[CFG_A])
    client, source = model_slots.resolve_slot_client(db, None, SLOT_REVIEW_DEFAULT)
    assert source == model_slots.SOURCE_ENV
    assert client.model == "env-default"


def test_thinking_mode_rejects_dirty_value():
    """脏值/mock 值一律按 follow,绝不当成 on/off 去改客户端"""
    db = FakeSession()
    db.store["slots"].append(SimpleNamespace(
        slot=SLOT_MEMORY_CURATOR, user_id="u1", llm_config_id=None,
        thinking_mode=MagicMock(),
    ))
    assert model_slots.get_slot_thinking_mode(db, "u1", SLOT_MEMORY_CURATOR) == "follow"


def test_slot_ignores_non_string_config_id():
    """非字符串 id(mock/脏值)视为未设置,不去查配置、也不覆盖旧列兜底"""
    db = FakeSession()
    db.store["slots"].append(SimpleNamespace(
        slot=SLOT_REVIEW_DEFAULT, user_id="u1", llm_config_id=MagicMock(),
        thinking_mode="follow",
    ))
    assert model_slots.get_slot_config_id(db, "u1", SLOT_REVIEW_DEFAULT) is None


def test_legacy_column_used_when_no_slot_row_at_all():
    """完全不传 legacy_value 时自取旧列(迁移没跑也不会丢用户设置)"""
    db = FakeSession(
        settings_rows={PracticeSettings: SimpleNamespace(
            user_id="u1", default_llm_config_id="cfg-a",
            thinking_mode_for_practice="off",
        )},
    )
    assert model_slots.get_slot_config_id(db, "u1", SLOT_PRACTICE_GENERATE) == "cfg-a"
    assert model_slots.get_slot_thinking_mode(db, "u1", SLOT_PRACTICE_GENERATE) == "off"


def test_user_llm_config_ids_membership():
    db = FakeSession(llm_configs=[{"id": "a"}, {"id": "b"}, {"nope": 1}])
    assert model_slots.user_llm_config_ids(db, "u1") == {"a", "b"}


def test_config_label_format():
    """展示口径与前端下拉一致:名称(厂商 / 模型);无名回退模型名"""
    assert model_slots.config_label(CFG_A) == "A(dashscope / qwen-max)"
    assert model_slots.config_label({"provider": "p", "model": "m"}) == "m(p / m)"
    assert model_slots.config_label(None) == ""


def test_orchestrator_build_client_chain(no_llm, monkeypatch):
    """任务级 > 任务默认审查模型槽位 > None(=agent 侧回退 env)

    这一条是本轮新接上的链路(以前任务没选模型就直接落环境变量)。
    orchestrator 自己引了一份 LLMClient(回退链两边都要用),两个模块都要替掉,
    否则会走真实构造(要求真 api_key + catalog 厂商元信息)而不是这里的假实现。
    """
    import app.agents.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "LLMClient", _FakeLLM)
    from app.agents.orchestrator import _build_llm_client

    db = FakeSession(
        llm_configs=[CFG_A, CFG_B],
        slots={SLOT_REVIEW_DEFAULT: ("cfg-b", "follow")},
    )
    # 1) 任务自带配置命中:用任务级
    client = _build_llm_client(
        db, "u1", "cfg-a", fallback_slot=SLOT_REVIEW_DEFAULT,
    )
    assert client is not None and client.model == "qwen-max"
    # 2) 任务自带配置已被删:不让位给 env,而是接槽位默认
    stale = _build_llm_client(db, "u1", "gone", fallback_slot=SLOT_REVIEW_DEFAULT)
    assert stale is not None and stale.model == "glm-4"
    # 3) 任务没选模型:用槽位默认
    from_slot = _build_llm_client(db, "u1", None, fallback_slot=SLOT_REVIEW_DEFAULT)
    assert from_slot is not None and from_slot.model == "glm-4"
    # 4) 两边都没配 → None(agent 自己构 env 默认,行为与拆二级前一致)
    assert _build_llm_client(
        FakeSession(llm_configs=[CFG_A]), "u1", None,
        fallback_slot=SLOT_REVIEW_DEFAULT,
    ) is None
    # 5) 不传 fallback_slot(其余调用点)时不引入新行为
    assert _build_llm_client(db, "u1", None) is None
    # 6) 匿名任务不查库直接 None
    assert _build_llm_client(
        db, None, "cfg-a", fallback_slot=SLOT_REVIEW_DEFAULT,
    ) is None


def test_react_client_source_reports_the_tier_actually_used(no_llm, monkeypatch):
    """排障日志的来源必须是「实际用了哪一级」。

    旧实现只要任务带过 react_llm_config_id 就固定报那个字段,而该 id 已被删时
    真正生效的是槽位默认 —— 对着错误的字段排障会找不到原因。"""
    import app.agents.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "LLMClient", _FakeLLM)
    from app.agents.orchestrator import CLIENT_FROM_SLOT, _build_react_llm_client

    db = FakeSession(
        llm_configs=[CFG_A, CFG_B],
        slots={SLOT_REVIEW_DEFAULT: ("cfg-b", "follow")},
    )

    def _task(react_id, llm_id=None):
        return SimpleNamespace(
            id="t1", user_id="u1",
            react_llm_config_id=react_id, llm_config_id=llm_id,
        )

    # 1) 任务自带 id 命中:报字段名
    client, label = _build_react_llm_client(db, _task("cfg-a"))
    assert client.model == "qwen-max" and label == "task.react_llm_config_id"
    # 2) 自带 id 已被删:实际走的是槽位默认,不能再报原字段
    client, label = _build_react_llm_client(db, _task("gone"))
    assert client.model == "glm-4" and label == "review_default 槽位"
    # 3) 没带 react id 但带了 llm_config_id:报回退字段名
    client, label = _build_react_llm_client(db, _task(None, "cfg-a"))
    assert client.model == "qwen-max" and label == "task.llm_config_id"
    # 4) 两边都没配但槽位有默认:报槽位
    client, label = _build_react_llm_client(db, _task(None, None))
    assert client.model == "glm-4" and label == "review_default 槽位"
    # 5) 全都没命中:报 env
    empty = FakeSession(llm_configs=[CFG_A])
    client, label = _build_react_llm_client(empty, _task(None, None))
    assert client is None and label == "env 默认"
    # 来源枚举与文案分层一致(枚举供其他调用方复用)
    assert orchestrator._resolve_llm_client(
        db, "u1", "gone", fallback_slot=SLOT_REVIEW_DEFAULT,
    )[1] == CLIENT_FROM_SLOT


def test_set_slot_semantics(no_llm):
    """get_or_create + None=不修改 / 空串=清空 / 脏思考模式忽略 / 不凭空建行"""
    db = FakeSession()
    user_id = db.user_id
    # 两侧都不写 → 不建行(避免一堆「全默认」空行)
    assert model_slots.set_slot(db, user_id, SLOT_REVIEW_DEFAULT) is None
    assert db.slot(SLOT_REVIEW_DEFAULT) is None

    row = model_slots.set_slot(db, user_id, SLOT_REVIEW_DEFAULT, llm_config_id="cfg-a")
    assert row.thinking_mode == "follow"  # 新建行补默认
    # 只改思考模式:已分配的模型不动
    model_slots.set_slot(db, user_id, SLOT_REVIEW_DEFAULT, thinking_mode="on")
    # 脏思考模式被忽略(不静默变成 follow 覆盖用户设置)
    model_slots.set_slot(db, user_id, SLOT_REVIEW_DEFAULT, thinking_mode="bogus")
    assert row.llm_config_id == "cfg-a"
    assert row.thinking_mode == "on"

    # 空串=清空回退 env:落成 CLEARED 标记而不是 NULL(NULL 会被启动迁移当成
    # 「还没从旧列交接完」而刷回旧值)
    model_slots.set_slot(db, user_id, SLOT_REVIEW_DEFAULT, llm_config_id="")
    assert row.llm_config_id == model_slots.CLEARED
    assert row.llm_config_id is not None
    # 始终同一行(get_or_create 没建成第二行)
    assert len([r for r in db.store["slots"] if r.slot == SLOT_REVIEW_DEFAULT]) == 1
    # 匿名用户不建行
    assert model_slots.set_slot(db, None, SLOT_REVIEW_DEFAULT, llm_config_id="cfg-a") is None


def test_set_slot_flushes_after_creating_row(no_llm):
    """autoflush 关闭:新建行后必须 flush 一次,否则同事务内重复调用会撞唯一约束"""
    db = FakeSession()
    assert db.flushes == 0
    model_slots.set_slot(db, db.user_id, SLOT_PRACTICE_GENERATE, llm_config_id="cfg-a")
    assert db.flushes == 1  # 建行后刷一次
    # 行已存时不再多刷(只改已有行不需要 flush)
    model_slots.set_slot(db, db.user_id, SLOT_PRACTICE_GENERATE, thinking_mode="on")
    assert db.flushes == 1
    # 不建行也不刷(两侧都没写)
    model_slots.set_slot(db, db.user_id, SLOT_MEMORY_CURATOR)
    assert db.flushes == 1


# ============================================================
# 注册表一致性(新增槽位时最容易漏的地方)
# ============================================================


def test_registry_covers_all_slot_ids():
    assert set(model_slots.MODEL_SLOTS) == set(model_slots.known_slots())
    assert set(model_slots.SLOT_ORDER) == set(model_slots.known_slots())


def test_registry_edit_paths_point_to_existing_panels():
    """编辑入口必须落在真实存在的设置子页(hash 写错会静默跳到一级页)"""
    expected = {
        SLOT_REVIEW_DEFAULT: "/settings/policy",
        SLOT_PRACTICE_GENERATE: "/settings/practice#generation",
        SLOT_PRACTICE_EXPLAIN: "/settings/practice#generation",
        SLOT_MEMORY_CURATOR: "/settings/memory",
    }
    for slot, path in expected.items():
        assert model_slots.MODEL_SLOTS[slot].edit_path == path


def test_only_review_default_is_writable_here():
    """其余槽位的写入口在归属面板端点,避免同一字段两处可写(后写覆盖)"""
    writable = {s for s, d in model_slots.MODEL_SLOTS.items() if d.writable}
    assert writable == {SLOT_REVIEW_DEFAULT}


def test_slot_views_marks_source_and_stale(no_llm):
    db = FakeSession(
        llm_configs=[CFG_A],
        slots={
            SLOT_REVIEW_DEFAULT: ("cfg-a", "follow"),
            SLOT_PRACTICE_GENERATE: ("gone", "follow"),
            SLOT_PRACTICE_EXPLAIN: (None, "follow"),
            SLOT_MEMORY_CURATOR: (None, "follow"),
        },
    )
    views = model_slots.slot_views(db, "u1")
    by_slot = {v["slot"]: v for v in views}
    assert by_slot[SLOT_REVIEW_DEFAULT]["source"] == "slot"
    assert by_slot[SLOT_REVIEW_DEFAULT]["config_label"] == "A(dashscope / qwen-max)"
    # 悬空引用:source 回落 env,同时置 stale 供前端提示
    assert by_slot[SLOT_PRACTICE_GENERATE]["source"] == "env"
    assert by_slot[SLOT_PRACTICE_GENERATE]["stale"] is True
    # 完全未设置:既不 stale 也没有 label
    assert by_slot[SLOT_MEMORY_CURATOR]["llm_config_id"] is None
    assert by_slot[SLOT_MEMORY_CURATOR]["stale"] is False
    # 顺序即注册表顺序(前端按此渲染,不再自己排)
    assert [v["slot"] for v in views] == list(model_slots.SLOT_ORDER)


# ============================================================
# 端点逻辑(会话替身,无需数据库)
# ============================================================


def _models_client(session, user_id=None):
    app = FastAPI()
    app.include_router(model_configs_router.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id if user_id is not None else session.user_id,
    )
    return TestClient(app)


def test_get_defaults_lists_slots_info_and_env_model(no_llm):
    session = FakeSession(llm_configs=[CFG_A])
    body = _models_client(session).get("/models/defaults").json()
    assert {s["slot"] for s in body["slots"]} == set(model_slots.known_slots())
    ids = {r["id"] for r in body["info_rows"]}
    assert {"prompt_polish", "embedding", "cli_agent"} <= ids
    assert body["env_default_model"] == settings.LLM_MODEL


def test_put_defaults_writes_and_clears_review_slot(no_llm):
    session = FakeSession(llm_configs=[CFG_A, CFG_B])
    client = _models_client(session)
    r = client.put("/models/defaults", json={
        "slots": {SLOT_REVIEW_DEFAULT: {"llm_config_id": "cfg-a"}}
    })
    assert r.status_code == 200
    view = {s["slot"]: s for s in r.json()["slots"]}
    assert view[SLOT_REVIEW_DEFAULT]["llm_config_id"] == "cfg-a"
    assert view[SLOT_REVIEW_DEFAULT]["source"] == "slot"

    # 空串=清空(回退 env)
    r2 = client.put("/models/defaults", json={
        "slots": {SLOT_REVIEW_DEFAULT: {"llm_config_id": ""}}
    })
    view2 = {s["slot"]: s for s in r2.json()["slots"]}
    assert view2[SLOT_REVIEW_DEFAULT]["llm_config_id"] is None
    assert view2[SLOT_REVIEW_DEFAULT]["source"] == "env"


def test_put_defaults_rejects_unknown_locked_foreign_and_thinking(no_llm):
    session = FakeSession(llm_configs=[CFG_A])
    client = _models_client(session)
    # 归属校验:配置不属于当前用户
    assert client.put("/models/defaults", json={
        "slots": {SLOT_REVIEW_DEFAULT: {"llm_config_id": "someone-else"}},
    }).status_code == 400
    # 白名单:出题槽位归练习设置面板写
    assert client.put("/models/defaults", json={
        "slots": {SLOT_PRACTICE_GENERATE: {"llm_config_id": "cfg-a"}},
    }).status_code == 400
    # 未知槽位
    assert client.put("/models/defaults", json={"slots": {"nope": {}}}).status_code == 400
    # 该槽位没有思考模式设置
    assert client.put("/models/defaults", json={
        "slots": {SLOT_REVIEW_DEFAULT: {"thinking_mode": "on"}},
    }).status_code == 400
    # 上面的失败都不该留下半成品行
    assert session.store["slots"] == []


def test_put_defaults_empty_request_is_noop(no_llm):
    """没带槽位时不写不建行(前端「只改一项」的载荷形态)"""
    session = FakeSession(llm_configs=[CFG_A])
    r = _models_client(session).put("/models/defaults", json={"slots": {}})
    assert r.status_code == 200
    assert session.store["slots"] == []


# ============================================================
# 数据库集成:迁移、旧端点写穿、preferences 投影
# ============================================================

pytestmark = pytest.mark.db  # 库不可达时由 tests/conftest.py 统一跳过
TEST_SCHEMA = f"pytest_model_slot_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = (
    "model_slot_defaults", "practice_settings", "memory_settings",
    "user_llm_configs", "users",
)


@pytest.fixture(scope="module")
def test_engine():
    try:
        # connect_timeout:远端库不可达时快速失败(否则 TCP 连接无超时会长挂),
        # 触发下方 pytest.skip 而非让整轮 pytest 卡死无输出。
        maint = create_engine(
            settings.DATABASE_URL, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"connect_timeout": 5, "options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(
            settings.DATABASE_URL, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:
        pass


@pytest.fixture()
def factory(test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture()
def db_ready(test_engine):
    """DB 用例统一入口:建 schema + 清表(纯逻辑用例不依赖它)"""
    with test_engine.begin() as conn:
        for t in _TABLES:
            conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
    return test_engine


def _seed_user(factory):
    db = factory()
    user = User(id=uuid.uuid4(), email="u@example.com", password_hash="x")
    db.add(user)
    db.add(UserLLMConfig(
        user_id=user.id,
        llm_configs=[
            {**CFG_A, "api_key": "k", "enable_thinking": True},
            {**CFG_B, "api_key": "k", "enable_thinking": False},
        ],
        embedding_configs=[],
    ))
    db.commit()
    db.refresh(user)
    db.close()
    return user


def _api_client(factory, user, router):
    app = FastAPI()
    app.include_router(router.router)

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _slots_of(factory, user_id):
    db = factory()
    rows = {r.slot: r for r in db.query(ModelSlotDefault).filter_by(user_id=user_id)}
    db.close()
    return rows


def test_migrate_copies_legacy_columns_and_is_idempotent(db_ready, factory, monkeypatch):
    """旧列拷进新表;重跑不覆盖用户在新表上的选择,也不产生重复行"""
    user = _seed_user(factory)
    db = factory()
    db.add(PracticeSettings(
        user_id=user.id, default_llm_config_id="cfg-a", thinking_mode_for_practice="on",
    ))
    db.add(MemorySettings(
        user_id=user.id, curator_llm_config_id="cfg-b", thinking_mode="off",
    ))
    db.commit()
    db.close()

    # 迁移函数内部 `from app.database import engine`,按属性替换到测试 schema
    monkeypatch.setattr("app.database.engine", db_ready)
    migrate_model_slot_defaults()

    rows = _slots_of(factory, user.id)
    assert rows[SLOT_PRACTICE_GENERATE].llm_config_id == "cfg-a"
    assert rows[SLOT_PRACTICE_GENERATE].thinking_mode == "on"
    assert rows[SLOT_MEMORY_CURATOR].llm_config_id == "cfg-b"
    assert rows[SLOT_MEMORY_CURATOR].thinking_mode == "off"

    # 用户在新表上改过(旧列仍是 cfg-a)后再跑迁移:不得被旧值刷回
    db = factory()
    db.query(ModelSlotDefault).filter_by(
        user_id=user.id, slot=SLOT_PRACTICE_GENERATE,
    ).first().llm_config_id = "cfg-b"
    db.commit()
    db.close()

    migrate_model_slot_defaults()

    rows = _slots_of(factory, user.id)
    assert rows[SLOT_PRACTICE_GENERATE].llm_config_id == "cfg-b"
    # 每槽位仍只有一行(迁移重跑不翻倍);practice_explain 旧列为空 → 不建行
    assert len(rows) == 2


def test_migrate_does_not_resurrect_a_cleared_slot(db_ready, factory, monkeypatch):
    """存量用户清空槽位的完整回归链(旧实现写 NULL 时这两步都中招):
    ① PUT 响应不得回显旧列值(否则前端下拉当场弹回)
    ② 重启重跑迁移不得把旧列值刷回来(否则「取消设置」永远失败)
    """
    user = _seed_user(factory)
    db = factory()
    db.add(PracticeSettings(
        user_id=user.id, default_llm_config_id="cfg-a", thinking_mode_for_practice="on",
    ))
    db.commit()
    db.close()

    monkeypatch.setattr("app.database.engine", db_ready)
    migrate_model_slot_defaults()  # 旧列值交接进新表(存量用户起点)

    client = _api_client(factory, user, memory_router)
    r = client.put("/memory/preferences/practice", json={
        "auto_generate_practice": True, "default_llm_config_id": "",
    })
    assert r.status_code == 200
    assert r.json()["default_llm_config_id"] is None

    migrate_model_slot_defaults()  # 模拟一次重启

    rows = _slots_of(factory, user.id)
    assert rows[SLOT_PRACTICE_GENERATE].llm_config_id == model_slots.CLEARED
    db = factory()
    # 旧列原样保留(新表是唯一写入口),但读路径已不再看它
    assert db.query(PracticeSettings).filter_by(
        user_id=user.id,
    ).first().default_llm_config_id == "cfg-a"
    assert model_slots.get_slot_config_id(db, user.id, SLOT_PRACTICE_GENERATE) is None
    db.close()


def test_migrate_skips_users_with_nothing_set(db_ready, factory, monkeypatch):
    """没设模型且思考模式为默认 follow:等价「未设置」,不产生空行"""
    user = _seed_user(factory)
    db = factory()
    db.add(PracticeSettings(user_id=user.id))
    db.commit()
    db.close()
    monkeypatch.setattr("app.database.engine", db_ready)
    migrate_model_slot_defaults()
    assert _slots_of(factory, user.id) == {}


def test_practice_settings_endpoint_writes_through_to_slot(db_ready, factory):
    """PUT /memory/preferences/practice 写槽位表,响应字段名不变(面板零改动)"""
    user = _seed_user(factory)
    client = _api_client(factory, user, memory_router)
    r = client.put("/memory/preferences/practice", json={
        "auto_generate_practice": True,
        "default_llm_config_id": "cfg-a",
        "thinking_mode_for_practice": "off",
        "explain_llm_config_id": "cfg-b",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["default_llm_config_id"] == "cfg-a"
    assert body["thinking_mode_for_practice"] == "off"
    assert body["explain_llm_config_id"] == "cfg-b"

    rows = _slots_of(factory, user.id)
    assert rows[SLOT_PRACTICE_GENERATE].llm_config_id == "cfg-a"
    assert rows[SLOT_PRACTICE_GENERATE].thinking_mode == "off"
    assert rows[SLOT_PRACTICE_EXPLAIN].llm_config_id == "cfg-b"
    # 旧列不再被写入(新表是唯一写入口)
    db = factory()
    settings_row = db.query(PracticeSettings).filter_by(user_id=user.id).first()
    assert settings_row.default_llm_config_id is None
    db.close()

    # 清空后再读:回退 env(字段仍在,值为 None)
    r2 = client.put("/memory/preferences/practice", json={
        "auto_generate_practice": True, "default_llm_config_id": "",
    })
    assert r2.json()["default_llm_config_id"] is None


def test_practice_settings_rejects_foreign_config(db_ready, factory):
    user = _seed_user(factory)
    client = _api_client(factory, user, memory_router)
    r = client.put("/memory/preferences/practice", json={
        "auto_generate_practice": True, "default_llm_config_id": "not-mine",
    })
    assert r.status_code == 400
    assert _slots_of(factory, user.id) == {}


def test_memory_settings_endpoint_writes_through_to_slot(db_ready, factory):
    user = _seed_user(factory)
    client = _api_client(factory, user, memory_router)
    r = client.put("/memory/preferences/memory_settings", json={
        "memory_enabled": True,
        "curator_llm_config_id": "cfg-b",
        "thinking_mode": "on",
    })
    assert r.status_code == 200
    ms = r.json()["memory_settings"]
    assert ms["curator_llm_config_id"] == "cfg-b"
    assert ms["thinking_mode"] == "on"

    rows = _slots_of(factory, user.id)
    assert (rows[SLOT_MEMORY_CURATOR].llm_config_id,
            rows[SLOT_MEMORY_CURATOR].thinking_mode) == ("cfg-b", "on")
    # 旧列不再被写入
    db = factory()
    row = db.query(MemorySettings).filter_by(user_id=user.id).first()
    assert row.curator_llm_config_id is None
    db.close()


def test_preferences_projection_reads_slots_not_legacy_column(db_ready, factory):
    """GET /memory/preferences 的模型字段来自槽位表(旧列即使有值也不该盖掉新表)"""
    user = _seed_user(factory)
    db = factory()
    db.add(PracticeSettings(user_id=user.id, default_llm_config_id="cfg-b"))  # 旧值
    db.add(ModelSlotDefault(
        user_id=user.id, slot=SLOT_PRACTICE_GENERATE,
        llm_config_id="cfg-a", thinking_mode="on",
    ))
    db.commit()
    db.close()

    client = _api_client(factory, user, memory_router)
    body = client.get("/memory/preferences").json()
    assert body["default_llm_config_id"] == "cfg-a"
    assert body["thinking_mode_for_practice"] == "on"
    assert body["memory_settings"]["curator_llm_config_id"] is None
