"""长期记忆管理相关的 Pydantic schema

对应 /memory 系列 API:
- UserPreferenceOut/SaveUserPreferenceRequest:User Profile (1:1)
- UserMemoryOut/SaveUserMemoryRequest:全局长期记忆(1:1)
- ProjectOut/SaveProjectRequest/ProjectListResponse:分项目记忆(1:N)

大小校验在 schema 层做第一道防线(写入时):
- user_profile ≤ 2000
- content(全局记忆) ≤ 20000
- memory_content(项目记忆) ≤ 20000

后续还有合并时截断(项目 8000 / 全局 10000)与注入时截断(2000)两道防线。
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.memory_settings import (
    DEFAULT_MEMORY_INJECT_MAX_CHARS,
    DEFAULT_MEMORY_STRUCTURE_MODE,
    MAX_MEMORY_CATEGORIES,
)
from app.models.practice import (
    THINKING_MODE_FOLLOW,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
)
from app.prompts.memory_curator import (
    DEFAULT_GLOBAL_CATEGORY_DEFS,
    DEFAULT_PROJECT_CATEGORY_DEFS,
)


class MemoryCategoryDef(BaseModel):
    """结构化类别定义(标题 + 描述)。

    描述用于指导归纳模型归类;类别列按顺序即优先级,末位为杂项桶。
    允许 title 为空(表格编辑态的临时空行),保存时由后端 _normalize_categories 过滤。
    """

    title: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=300)


class MemorySettingsOut(BaseModel):
    """记忆生成设置响应(嵌在 UserPreferenceOut.memory_settings 内)

    项目/全局结构化类别在建行时即播种为内置默认,故响应里的类别列就是生效值。
    (内置默认列表本身不随本响应携带——它是全体用户共享的静态常量,
    由 GET /memory/preferences/structure_defaults 单独提供,供前端对照与恢复。)
    """

    memory_enabled: bool = True
    # 记忆归纳/精简专用模型(UserLLMConfig 配置 id;None=未指定,回退 env 默认)
    curator_llm_config_id: str | None = None
    # 归纳/精简思考模式(follow/on/off,默认 follow)
    thinking_mode: str = THINKING_MODE_FOLLOW
    # 结构化预设模式:structured / freeform
    structure_mode: str = DEFAULT_MEMORY_STRUCTURE_MODE
    # 结构化类别(项目/全局各一套;按顺序即优先级,末位为杂项桶)
    project_categories: list[MemoryCategoryDef] = Field(
        default_factory=lambda: [MemoryCategoryDef(**d) for d in DEFAULT_PROJECT_CATEGORY_DEFS]
    )
    global_categories: list[MemoryCategoryDef] = Field(
        default_factory=lambda: [MemoryCategoryDef(**d) for d in DEFAULT_GLOBAL_CATEGORY_DEFS]
    )
    # 精简版记忆注入字符上限(超出调 LLM 精简/截断)
    inject_max_chars: int = DEFAULT_MEMORY_INJECT_MAX_CHARS

    model_config = {"from_attributes": True}


class StructureDefaultsOut(BaseModel):
    """系统默认结构化类别(GET /memory/preferences/structure_defaults)

    内置默认是全体用户共享的静态常量(与 prompts/memory_curator.py 的 DEFAULT_*_CATEGORY_DEFS 同源);
    单独一个轻量只读端点提供,避免塑进每次 preferences 响应。
    """

    project_categories: list[MemoryCategoryDef] = Field(
        default_factory=lambda: [MemoryCategoryDef(**d) for d in DEFAULT_PROJECT_CATEGORY_DEFS]
    )
    global_categories: list[MemoryCategoryDef] = Field(
        default_factory=lambda: [MemoryCategoryDef(**d) for d in DEFAULT_GLOBAL_CATEGORY_DEFS]
    )
    # 结构化类别数量上限(与保存校验同源,前端据此限制添加)
    max_categories: int = MAX_MEMORY_CATEGORIES


class SaveMemorySettingsRequest(BaseModel):
    """保存记忆设置请求(PUT /memory/preferences/memory_settings)

    - memory_enabled:自动归纳总开关
    - curator_llm_config_id:记忆专用模型配置 id;None=本次不修改,空串=清空(回退 env)
      (归属校验:必须是当前用户已保存的 LLM 配置)
    - thinking_mode / structure_mode / inject_max_chars:None 表示本次不修改
    - project_categories / global_categories:None 表示本次不修改;传列表则整体覆盖
      (最多 MAX_MEMORY_CATEGORIES 个;空列表表示清空,服务层会回退内置默认)
    """

    memory_enabled: bool = True
    curator_llm_config_id: str | None = Field(default=None, max_length=36)
    thinking_mode: Literal[
        THINKING_MODE_FOLLOW, THINKING_MODE_ON, THINKING_MODE_OFF,
    ] | None = None
    structure_mode: Literal["structured", "freeform"] | None = None
    inject_max_chars: int | None = Field(default=None, ge=200, le=8000)
    project_categories: list[MemoryCategoryDef] | None = Field(
        default=None, max_length=MAX_MEMORY_CATEGORIES
    )
    global_categories: list[MemoryCategoryDef] | None = Field(
        default=None, max_length=MAX_MEMORY_CATEGORIES
    )


class UserPreferenceOut(BaseModel):
    """User Profile 响应(GET /memory/preferences)"""

    # 自由文本 Markdown(用户在记忆管理页编辑,注入 agent2)
    user_profile: str = ""
    # agent 策略配置(agent2 启停、验证权限等)
    # None 表示未配置(用系统默认),dict 表示用户自定义的覆盖值
    agent_policy: dict[str, Any] | None = None
    # 任务完成后是否自动生成练习题 draft(默认开)
    auto_generate_practice: bool = True
    # 出题前沙箱已清理时是否重新 clone 恢复工作区(默认关)
    restore_workspace_for_practice: bool = False
    # 用户级默认出题模型(UserLLMConfig 配置 id;None=未设置,回退任务级/env 默认)
    default_llm_config_id: str | None = None
    # 始终用默认出题模型(忽略任务自带模型配置;默认关)
    force_default_llm: bool = False
    # 出题思考模式覆盖(follow=跟随模型配置/on=强制开/off=强制关,默认 follow)
    thinking_mode_for_practice: str = THINKING_MODE_FOLLOW
    # 记忆生成设置(总是返回;未配置行时为预置默认,供前端播种类别编辑器)
    memory_settings: MemorySettingsOut = Field(default_factory=MemorySettingsOut)
    # 最后更新时间(可空 — 未配置时为 None;FastAPI 序列化为 ISO 字符串)
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SaveUserPreferenceRequest(BaseModel):
    """保存 User Profile 请求(PUT /memory/preferences)"""

    user_profile: str = Field(default="", max_length=2000)


class SavePracticeSettingsRequest(BaseModel):
    """保存练习设置请求(PUT /memory/preferences/practice)

    - auto_generate_practice:任务完成后是否自动生成练习题 draft
      (产出仍需用户在预览对话框确认才转 active)
    - restore_workspace_for_practice:出题前沙箱已清理时是否重新 clone
      恢复工作区;None 表示本次不修改
    - default_llm_config_id:用户级默认出题模型(UserLLMConfig 配置 id);
      None 表示本次不修改,空串表示清空(回退任务级/env 默认)
    - force_default_llm:始终用默认出题模型(忽略任务自带模型配置);
      None 表示本次不修改
    - thinking_mode_for_practice:出题思考模式覆盖(follow/on/off);
      None 表示本次不修改

    (learning_topic 已移除:出题主题现按发现内容自动匹配)
    """

    auto_generate_practice: bool = True
    restore_workspace_for_practice: bool | None = None
    default_llm_config_id: str | None = Field(default=None, max_length=36)
    force_default_llm: bool | None = None
    thinking_mode_for_practice: Literal[
        THINKING_MODE_FOLLOW,
        THINKING_MODE_ON,
        THINKING_MODE_OFF,
    ] | None = None


class SaveAgentPolicyRequest(BaseModel):
    """保存 agent 策略配置请求(PUT /memory/preferences/agent_policy)

    结构与 agent_policy.DEFAULT_AGENT_POLICY 对齐:
    - agent2_enabled: 是否启用 agent2(关闭=单 agent 模式)
    - allow_verify: agent2 是否能调用 verifier_agent 验证(需任务配了 test_env_url)
    - allow_reference_check: agent2 是否能复核 agent1 引用的网址
      (后端安全抓取 + SSRF 防护;结果仅供参考信号)
    - verifier_auth_mode_default: 验证授权默认模式("direct"直接执行 / "per_action"逐动作授权)
    - executor_command_confirm_default: 执行智能体命令确认默认模式
        "always_approve" 自动批准所有命令 / "per_command" 每个危险命令弹窗确认

    历史:曾有 max_rounds(协作总轮次)字段,agent2 审查移到后台执行后已移除。
    """

    agent2_enabled: bool = True
    allow_verify: bool = False
    allow_reference_check: bool = True
    verifier_auth_mode_default: str = Field(default="per_action", pattern="^(direct|per_action)$")
    executor_command_confirm_default: str = Field(
        default="always_approve", pattern="^(always_approve|per_command)$"
    )


class UserMemoryOut(BaseModel):
    """全局长期记忆响应(GET /memory/global)"""

    content: str = ""
    # 最后更新时间(可空 — 未配置时为 None;FastAPI 序列化为 ISO 字符串)
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SaveUserMemoryRequest(BaseModel):
    """保存全局长期记忆请求(PUT /memory/global)"""

    content: str = Field(default="", max_length=20000)


class ProjectOut(BaseModel):
    """分项目记忆响应(GET /memory/projects、GET/PUT /memory/projects/{id})"""

    id: str
    repo_url_normalized: str
    repo_url_raw: str
    alias: str | None = None
    note: str | None = None
    memory_content: str = ""
    # 精简版记忆(系统生成,注入 system prompt 用;前端可查看)
    memory_summary: str = ""
    # 上次自动归纳时间(ISO 字符串,可空)
    last_summary_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class SaveProjectRequest(BaseModel):
    """保存分项目记忆请求(PUT /memory/projects/{id})

    仅允许编辑 alias/note/memory_content(不修改 repo_url 与 last_summary_at)。
    """

    alias: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None)
    memory_content: str = Field(default="", max_length=20000)


class ProjectListResponse(BaseModel):
    """分项目记忆列表响应(GET /memory/projects、DELETE 后响应)"""

    projects: list[ProjectOut] = Field(default_factory=list)
