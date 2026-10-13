"""报告导出"审查结果"节单元测试(_append_review_items_md/html)

验证:ReviewItem 按三分档渲染 + 待核实对象/审查结论/证据链/置信 + HTML 转义,
并锚定"报告正文不直铺内部枚举"(status=covered · verdict=confirmed 用户读不出含义,
来源与严重度一律走中文标签)。用内存态 ReviewItem(不落库),bucket 属性现算。
"""
# 单跑本文件时注册全部 mapper 依赖(User→UserGitBinding 等字符串关系需被导入才配置)
import app.models.task_artifact  # noqa: F401
import app.models.user_git_binding  # noqa: F401
from app.models.audit import ReviewItem
from app.routers.tasks import _append_review_items_html, _append_review_items_md


def _item(**over):
    base = dict(
        task_id=None, round_idx=1, title="SQL 注入",
        review_target="users.py 存在注入", origin="agent1_claim",
        status="covered", verdict="confirmed", severity="high",
        conclusion="读了 src/users.py:42,拼接 SQL 未经参数化,成立。",
        confidence={"tier": "source_confirmed", "label": "源码取证"},
        evidence={"source": {"file_path": "src/users.py", "line": "42", "quote": "cur.execute(sql)"}},
        evidence_mismatch=False, follow_up="如何把这条查询改成参数化查询?",
    )
    base.update(over)
    return ReviewItem(**base)


def test_markdown_sections_and_buckets():
    lines: list[str] = []
    _append_review_items_md(lines, [
        _item(),
        _item(title="已核实不成立", verdict="none", severity=None, confidence=None,
              evidence=None, conclusion="该指控不成立,配置项已有校验。"),
        _item(title="并发核实到一半", status="partial", verdict="pending",
              origin="domain_baseline", review_target="并发", evidence=None,
              confidence=None, conclusion="仅静态分析,未取到证。"),
    ])
    text = "\n".join(lines)
    assert "## 审查结果" in text
    assert "### 已核实成立 (1)" in text
    assert "### 已核实不成立 (1)" in text
    assert "### 待核实 (1)" in text
    # 置信(带证据) + 无证据(未取证,旧文案"未分级"读不出原因)
    assert "源码取证" in text
    assert "78%" not in text  # 百分比已去掉,只留档位标签
    assert "无证据(未取证)" in text
    assert "原始证据(src/users.py:42)" in text
    # 字段新语义与中文标签
    assert "待核实对象:users.py 存在注入" in text
    assert "审查结论:" in text
    assert "可追问内容:如何把这条查询改成参数化查询?" in text
    assert "来源:AI助手 的结论" in text
    assert "严重度:高" in text
    # 不再直铺内部枚举
    assert "status=covered" not in text
    assert "判定:confirmed" not in text
    assert "建议:" not in text


def test_markdown_empty_no_section():
    lines: list[str] = []
    _append_review_items_md(lines, [])
    assert lines == []


def test_html_escapes_and_new_fields():
    parts: list[str] = []
    _append_review_items_html(parts, [
        _item(title="<script>x</script>", review_target="<b>&", evidence=None,
              confidence=None, follow_up="<em>追问</em>"),
    ])
    text = "".join(parts)
    assert "<h2>审查结果</h2>" in text
    assert "<script>" not in text  # 已转义
    assert "&lt;script&gt;" in text
    assert "待核实对象:&lt;b&gt;&amp;" in text
    assert "可追问内容:&lt;em&gt;追问&lt;/em&gt;" in text
    assert "来源:AI助手 的结论" in text
