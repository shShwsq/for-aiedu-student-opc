"""UserGitBinding 反向关系回归测试(纯模型自省,不连真实 DB)。

背景:/auth/oauth/{provider} 登录用 `binding.user` 取回本地用户
(auth.py git_oauth),但 UserGitBinding 侧曾缺失该反向关系,访问即抛
AttributeError → 接口 500「请求失败(500)」,凡是已有绑定行的用户
(GitHub / Gitee 登录过或迁移脚本搬进来的)永远登录不了。

本测试固化三条契约:
1. UserGitBinding.user 存在且指向 User(多对一标量)
2. 两侧成对:绑定行挂到 user 上即出现在 user.git_bindings 中
3. 父侧级联仍走数据库 FK(passive_deletes=True),避免反向关系的加入
   让 ORM 在删用户时对子行发 UPDATE ... SET NULL(user_id NOT NULL → 报错)
"""
import app.models.email_token  # noqa: F401  注册全部 mapper
import app.models.project  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.user  # noqa: F401
import app.models.user_agent_config  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_llm_config  # noqa: F401
import app.models.user_memory  # noqa: F401
import app.models.user_preference  # noqa: F401
from sqlalchemy.orm import configure_mappers

from app.models.user import User
from app.models.user_git_binding import UserGitBinding


def test_binding_has_user_relationship():
    """UserGitBinding.user 必须存在(缺失会让 OAuth 登录端点 500)。"""
    configure_mappers()  # 关系字符串目标解析不过会在这里报错
    assert UserGitBinding.__mapper__.has_property("user"), (
        "UserGitBinding 缺少 user 反向关系:auth.py git_oauth 用 binding.user "
        "取本地用户,缺失会抛 AttributeError → POST /auth/oauth/{provider} 500"
    )
    rel = UserGitBinding.__mapper__.relationships["user"]
    assert rel.mapper.class_ is User, "user 关系应指向 User"
    assert rel.uselist is False, "多对一:binding.user 必须是标量而非列表"


def test_binding_user_is_bidirectional_with_git_bindings():
    """binding.user 与 user.git_bindings 双向同步(成对关系而非两条独立查询)。"""
    # 方向 1:binding.user = user → 出现在 user.git_bindings 中
    user = User(email="u@example.com", password_hash="x")
    binding = UserGitBinding(
        provider="gitee",
        provider_user_id="16747772",
        access_token="",
        user=user,
    )
    assert binding.user is user
    assert binding in user.git_bindings, (
        "两侧未成对:绑定行挂到 user 上但 User.git_bindings 看不到它"
    )

    # 方向 2:user.git_bindings.append(binding) → binding.user 被设置。
    # 仅子侧声明 back_populates 时此方向静默失效(父侧也必须声明,
    # 否则 append 后同事务内读 binding.user 会拿到 None)
    user2 = User(email="u2@example.com", password_hash="x")
    binding2 = UserGitBinding(
        provider="github",
        provider_user_id="42",
        access_token="",
    )
    user2.git_bindings.append(binding2)
    assert binding2.user is user2, (
        "父侧 git_bindings 缺 back_populates='user':append 后 binding.user "
        "未被同步设置(单向 back_populates 只在子→父方向生效)"
    )


def test_git_bindings_cascade_stays_db_level():
    """父侧 delete-orphan 必须配 passive_deletes=True,由 FK ondelete=CASCADE 删子行。

    加了反向关系后若去掉 passive_deletes,删账号时 SQLAlchemy 会对子行发
    UPDATE user_id=NULL(NOT NULL 列)或额外 DELETE,与既有级联约定冲突。
    """
    rel = User.__mapper__.relationships["git_bindings"]
    assert rel.passive_deletes is True, "passive_deletes 必须为 True(级联交给数据库)"
    # SQLAlchemy 2.x 迭代 cascade 得到策略名字符串,统一 str() 兼容两种形态
    cascades = {str(c) for c in rel.cascade}
    assert {"delete", "delete-orphan"} <= cascades, f"级联策略应含 delete/delete-orphan,实际 {cascades}"
