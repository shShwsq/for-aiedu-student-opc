"""报告导出"审查结果"节单元测试(_append_review_items_md/html)

验证:ReviewItem 按三态分桶渲染 + 被核实对象/证据链/置信 + HTML 转义。
用内存态 ReviewItem(不落库),bucket 属性由 status/verdict 现算。
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
        confidence={"tier": "source_confirmed", "label": "源码取证", "score": 0.78},
        evidence={"source": {"file_path": "src/users.py", "line": "42", "quote": "cur.execute(sql)"}},
        evidence_mismatch=False, suggestion="改参数化查询",
    )
    base.update(over)
    return ReviewItem(**base)


def test_markdown_sections_and_buckets():
    lines: list[str] = []
    _append_review_items_md(lines, [
        _item(),
        _item(title="已核查无问题", verdict="none", severity=None, confidence=None),
        _item(title="缺并发审查", status="missing", verdict="pending", origin="domain_baseline",
              review_target="并发", evidence=None, suggestion="建议追问并发"),
    ])
    text = "\n".join(lines)
    assert "## 审查结果" in text
    assert "### 发现风险 (1)" in text
    assert "### 已核查·剔除误报 (1)" in text
    assert "### 缺口·待改进 (1)" in text
    # 置信(带证据) + 未分级(缺口无 evidence)
    assert "源码取证" in text
    assert "未分级" in text
    assert "原始证据(src/users.py:42)" in text


def test_markdown_empty_no_section():
    lines: list[str] = []
    _append_review_items_md(lines, [])
    assert lines == []


def test_html_escapes():
    parts: list[str] = []
    _append_review_items_html(parts, [
        _item(title="<script>x</script>", review_target="<b>&", evidence=None, confidence=None),
    ])
    text = "".join(parts)
    assert "<h2>审查结果</h2>" in text
    assert "<script>" not in text  # 已转义
    assert "&lt;script&gt;" in text
