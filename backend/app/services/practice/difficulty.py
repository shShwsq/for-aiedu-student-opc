"""题目难度评估与用户能力估计

- 题目难度 1-5:LLM 生成时初评;作答后按「能力-难度-结果」三角微调
- 用户能力值:冷启动 2.5;之后为最近 10 次作答(答对+答错)题目难度的加权均值
  (越近的作答权重越高,反映当前水平)
  答对以该题难度为目标值,答错以「难度 - PENALTY」为目标值(失败把能力往下拉,
  修正旧版只统计答对题导致能力值系统性偏高、越出越难)。
"""

DIFFICULTY_MIN = 1.0
DIFFICULTY_MAX = 5.0
COLD_START_ABILITY = 2.5
# 能力估计的作答样本窗口(答对+答错都计入)
ABILITY_WINDOW = 10
# 分面能力估计扫描的最近作答上限(分组前先从 DB 取的行数,兼顾精度与开销)
FACET_ABILITY_SCAN = 200
# 答错题对能力估计的扣减:失败说明该难度当前不可及,目标值下探
ABILITY_WRONG_PENALTY = 1.0


def clamp_difficulty(value: float) -> float:
    return max(DIFFICULTY_MIN, min(DIFFICULTY_MAX, float(value)))


def adjust_question_difficulty(
    difficulty: float, ability: float, is_correct: bool
) -> float:
    """作答后微调题目难度

    - 能力不低于该题难度却答错 → 题目标高了不算,反而可能说明题目有迷惑性,
      视为题目比预期难:+0.5
    - 能力远低于难度却答对 → 题目比预期简单:-0.5
    - 其余情况(结果与预期一致)不调整
    """
    d = clamp_difficulty(difficulty)
    if not is_correct and ability >= d:
        return clamp_difficulty(d + 0.5)
    if is_correct and ability <= d - 1.5:
        return clamp_difficulty(d - 0.5)
    return d


def estimate_ability(samples: list[tuple[float, bool]]) -> float:
    """由最近作答(答对+答错)的难度序列估计能力值

    入参为 (difficulty, is_correct) 元组,按作答时间升序(旧→新);
    只取最后 ABILITY_WINDOW 条。权重线性递增:第 i 条(0 起)权重 i+1,
    越近越重要。答对目标值取难度本身,答错取「难度 - ABILITY_WRONG_PENALTY」
    (失败把能力往下拉)。结果钳制到 [1,5]。无作答记录 → 冷启动 2.5。
    """
    recent = samples[-ABILITY_WINDOW:]
    if not recent:
        return COLD_START_ABILITY
    total_w = 0.0
    total = 0.0
    for i, (d, is_correct) in enumerate(recent):
        target = clamp_difficulty(d) if is_correct else max(
            DIFFICULTY_MIN, clamp_difficulty(d) - ABILITY_WRONG_PENALTY
        )
        w = i + 1
        total += target * w
        total_w += w
    if not total_w:
        return COLD_START_ABILITY
    return clamp_difficulty(total / total_w)
