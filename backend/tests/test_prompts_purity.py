"""app/prompts 纯文本资产包的零依赖约束测试。

app/prompts 定位为纯文本层(常量/模板/纯格式化函数),不得 import 任何
app.* 模块:数据加载在 memory_injection 等服务层,消息装配在各执行器。
本测试用 ast 静态扫描固化该约束,防止后续往 prompts 里塞执行逻辑
形成 import 环或隐式耦合。
"""
import ast
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "app" / "prompts"


def _collect_app_imports(path: Path) -> list[str]:
    """收集单个 py 文件里的 app.* import(含相对 import)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    offenders.append(f"{path.name}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                offenders.append(f"{path.name}: 相对 import(from {'.' * node.level}{node.module or ''})")
            elif (node.module or "").startswith("app.") or node.module == "app":
                offenders.append(f"{path.name}: from {node.module} import ...")
    return offenders


def test_prompts_package_has_no_app_imports():
    """app/prompts 下所有 .py 文件零 app.* import(纯文本层硬约束)。"""
    files = sorted(PROMPTS_DIR.glob("*.py"))
    assert files, "app/prompts 包应存在且至少含 __init__.py"
    offenders: list[str] = []
    for f in files:
        offenders.extend(_collect_app_imports(f))
    assert offenders == [], (
        "app/prompts 必须零 app.* 依赖(数据加载放服务层,装配放执行器): "
        + "; ".join(offenders)
    )


def test_prompts_modules_importable_and_nonempty():
    """各 prompt 模块可导入且非空(防搬家后出现空壳/拼写错误)。"""
    from app.prompts import (  # noqa: F401
        agent2, executor, memory_curator, polish, practice, verifier,
    )

    assert agent2.AGENT2_REVIEW_PROMPT.strip()
    assert executor.REACT_AGENT_SYSTEM_PROMPT.strip()
    assert memory_curator._SUMMARIZE_PROMPT.strip()
    assert practice.build_system_prompt("security", False).strip()
    assert verifier.VERIFIER_SYSTEM_PROMPT.strip()
    assert polish.POLISH_SYSTEM_PROMPT.strip()
    assert polish.build_polish_messages("你好")[0]["content"].strip()
