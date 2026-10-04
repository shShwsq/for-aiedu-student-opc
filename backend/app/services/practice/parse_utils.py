"""LLM 输出容错解析小工具(json_repair 之上的文本级挽救)

单独成模块的原因:出题(generator)与知识点讲解(explainer)都需要
「模型在 JSON 数组前后夹带散文」的挽救逻辑(思考类模型很常见),
而两者互相引用会形成循环依赖。
"""


def salvage_json_array(text: str) -> str | None:
    """从混合文本中抠出第一个括号平衡的 JSON 数组片段

    背景:思考类模型(deepseek-r1 等)常把「出题思路」写在 JSON 数组前面,
    repair_json 于是返回字符串 → 整条结果被当成非法输出丢弃。
    这里字符串字面量/转义感知地扫描,抽第一个完整 [ ... ] 交给调用方重解。
    抽不到返回 None(维持原丢弃行为)。
    """
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
