"""LLM 客户端封装

统一 OpenAI 兼容后端,所有厂商(DashScope/DeepSeek/智谱/Kimi/豆包/MiniMax)走同一套调用代码,
差异通过 models_catalog.json 描述:
- thinkingParam:思考参数名(enable_thinking 或 thinking)
- thinkingEnabledType:开启时的取值(enabled 或 adaptive)
- reasoningSplit:是否需要额外传 reasoning_split(MiniMax)
- thinkingTemperature/nonThinkingTemperature:思考/非思考模式不同温度(Kimi)
- thinking 模式:hybrid(可开关)/ only(强制)/ none(不支持)

流式:chat_stream() 返回生成器,逐 chunk 产出 (reasoning_delta, content_delta, tool_call_delta)。
- reasoning_delta:思考链增量(DeepSeek-R1 / Qwen-QwQ / Kimi-k2.6 等模型才有)
- content_delta:正式回答增量
- tool_call_delta:工具调用增量(累积 index + arguments 片段)
所有 LLM 调用统一走流式,前端通过 SSE 实时看到思考过程。
"""
import json
import logging
import random
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, Callable

import openai
from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)


def _close_stream(stream: Any) -> None:
    """关闭 OpenAI 流式响应(用户终止调用时在 chunk 边界提前收流)

    openai.Stream 包着 httpx.Response,不显式 close 则连接回到池前会始终挂着
    (底层 SSE 未读完)。关闭失败不影响调用方 —— 已经拿到的增量照常有用。
    """
    try:
        stream.close()
    except AttributeError:
        pass  # 非标准流对象(测试替身等):无 close 可调
    except Exception as e:
        logger.debug(f"关闭流式响应失败(忽略): {str(e)[:200]}")


# ============================================================
# 厂商清单加载与查找
# ============================================================

_catalog: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    """加载 models_catalog.json(进程级缓存)"""
    global _catalog
    if _catalog is not None:
        return _catalog
    catalog_path = Path(__file__).parent / "models_catalog.json"
    with catalog_path.open(encoding="utf-8") as f:
        _catalog = json.load(f)
    return _catalog


def find_provider(provider_id: str) -> dict[str, Any] | None:
    """按 id 查找厂商元信息"""
    catalog = _load_catalog()
    for p in catalog.get("llmProviders", []):
        if p["id"] == provider_id:
            return p
    return None


def find_model_meta(provider: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    """在 provider 内查找模型元信息(thinking 模式等)"""
    for m in provider.get("models", []):
        if m["id"] == model_id:
            return m
    return None


# ============================================================
# thinking 参数构造
# ============================================================


def build_thinking_extras(
    provider: dict[str, Any],
    model_meta: dict[str, Any] | None,
    enable_thinking: bool,
) -> dict[str, Any] | None:
    """构造思考参数注入字典

    返回 None 表示该模型/厂商不支持思考或不需要注入

    自定义模型(不在 catalog 中,model_meta 为 None)的处理:
    - 厂商支持思考 → 按 hybrid 模式处理,尊重 enable_thinking 开关
      (避免厂商默认开启思考却未显式关闭,如 DashScope qwen3 系列)
    - 厂商不支持思考 → 不注入
    """
    if not provider.get("supportsThinking"):
        return None

    # thinking 模式:hybrid(可开关)/ only(强制)/ none(不支持)
    thinking_mode = model_meta.get("thinking") if model_meta else None
    if not thinking_mode:
        # 豆包等厂商用 Endpoint ID,model_meta 可能匹配不上,用 provider 兜底
        thinking_mode = provider.get("fallbackThinking", "hybrid")

    if thinking_mode == "none":
        return None

    param_name = provider.get("thinkingParam", "enable_thinking")
    enabled_type = provider.get("thinkingEnabledType", "enabled")

    extras: dict[str, Any] | None = None

    if thinking_mode == "only":
        # 仅思考模式:强制开启
        if param_name == "thinking":
            extras = {"thinking": {"type": enabled_type}}
        else:
            extras = {param_name: True}
    elif thinking_mode == "hybrid":
        # 混合思考:按开关显式传值(关闭时必须显式 false/disabled)
        if param_name == "thinking":
            extras = {"thinking": {"type": enabled_type if enable_thinking else "disabled"}}
        else:
            extras = {param_name: enable_thinking}

    # MiniMax 需额外传 reasoning_split: true
    if extras and provider.get("reasoningSplit"):
        extras["reasoning_split"] = True

    return extras


# ============================================================
# 输出上限(max_tokens)解析
# ============================================================

# 系统默认单次输出上限。agent2 的 done=true 评估要输出 results+grouping,
# 上限太低会被截断导致 JSON 解析失败;16384 对绝大多数评估输出已足够。
DEFAULT_MAX_OUTPUT_TOKENS = 16384


def resolve_output_limit(
    provider: dict[str, Any],
    model_meta: dict[str, Any] | None,
    explicit_limit: int | None = None,
) -> int:
    """解析模型单次输出上限(max_tokens 钳制值)

    优先级:用户显式设置 > catalog 模型级 outputLimit >
    provider 级 fallbackOutputLimit(豆包 Endpoint ID 等匹配不上模型时用)>
    系统默认 DEFAULT_MAX_OUTPUT_TOKENS。

    catalog 中的 outputLimit 按各模型官方能力保守取值,
    避免传入超过模型输出能力的值导致 API 400。
    """
    if explicit_limit is not None and explicit_limit > 0:
        return explicit_limit
    if model_meta:
        limit = model_meta.get("outputLimit")
        if isinstance(limit, int) and limit > 0:
            return limit
    fallback = provider.get("fallbackOutputLimit")
    if isinstance(fallback, int) and fallback > 0:
        return fallback
    return DEFAULT_MAX_OUTPUT_TOKENS


def resolve_temperature(
    provider: dict[str, Any],
    model_meta: dict[str, Any] | None,
    enable_thinking: bool,
    explicit_temperature: float | None = None,
) -> float:
    """解析温度

    优先级:显式传入 > provider 思考模式动态值 > 默认 0.7
    Kimi k2.6/k2.5:思考模式固定 1.0,非思考模式固定 0.6
    """
    if explicit_temperature is not None:
        return explicit_temperature

    thinking_temp = provider.get("thinkingTemperature")
    non_thinking_temp = provider.get("nonThinkingTemperature")
    if thinking_temp is not None or non_thinking_temp is not None:
        thinking_mode = model_meta.get("thinking") if model_meta else None
        if not thinking_mode:
            thinking_mode = provider.get("fallbackThinking", "none")
        is_thinking = thinking_mode == "only" or (
            thinking_mode == "hybrid" and enable_thinking
        )
        if is_thinking:
            return thinking_temp if thinking_temp is not None else 1.0
        return non_thinking_temp if non_thinking_temp is not None else 0.7

    return provider.get("defaultTemperature", 0.7)


# ============================================================
# 429 限流退避重试
# ============================================================

# 自算退避的参数:base * 2^attempt(带抖动),封顶 _BACKOFF_MAX 秒;
# 厂商返回 Retry-After 时优先采用(封顶 _RETRY_AFTER_MAX 秒)。
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 20.0
_RETRY_AFTER_MAX = 60.0


def _parse_retry_after(exc: Exception) -> float | None:
    """从 429 异常的响应头解析 Retry-After(秒),缺失/非法返回 None"""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), _RETRY_AFTER_MAX))
    except (TypeError, ValueError):
        return None


def create_with_rate_limit_retry(create_fn, *, label: str):
    """带 429 退避重试地调用 create_fn()

    仅处理请求发起阶段的 openai.RateLimitError(RPM/配额限流);
    连接错误/5xx 等仍由 openai SDK 内置重试处理,不在此重叠。
    首次之外最多重试 settings.LLM_RATE_LIMIT_MAX_RETRIES 次,
    耗尽后原样重抛,由上层处理(任务失败/测试提示)。

    退避策略:厂商响应头带 Retry-After 时优先采用(视为等待下限,不加抖动);
    否则指数退避 base*2^attempt 并带 ±25% 抖动,避免多任务同步重试再次撞限。
    """
    max_retries = max(0, settings.LLM_RATE_LIMIT_MAX_RETRIES)
    for attempt in range(max_retries + 1):
        try:
            return create_fn()
        except openai.RateLimitError as e:
            if attempt >= max_retries:
                logger.warning(
                    "%s 命中 429 限流,已重试 %d 次仍失败,放弃: %s", label, max_retries, e
                )
                raise
            retry_after = _parse_retry_after(e)
            if retry_after is not None:
                delay = retry_after
            else:
                delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
                delay *= 0.75 + random.random() * 0.5  # ±25% 抖动
            logger.info(
                "%s 命中 429 限流,%.1fs 后重试(%d/%d)%s",
                label, delay, attempt + 1, max_retries,
                "(Retry-After)" if retry_after is not None else "",
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # 仅为类型检查兜底


# ============================================================
# OpenAI 兼容 baseUrl 拼接
# ============================================================


def build_chat_url(base_url: str) -> str:
    """拼接 chat/completions 端点

    若 baseUrl 已含版本前缀(/v1、/v2、/v3、/v4),直接拼 /chat/completions;
    否则补 /v1(纯 baseUrl 的厂商,如 MiniMax)
    """
    base = (base_url or "https://api.openai.com").rstrip("/")
    import re

    if re.search(r"/v\d+$", base):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


# ============================================================
# LLM 客户端
# ============================================================


class LLMClient:
    """OpenAI 兼容 LLM 客户端

    配置来源优先级:显式构造参数 > 用户保存的配置(阶段 6) > env 默认(阶段 1)
    """

    def __init__(
        self,
        provider_id: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
        base_url: str | None = None,
        max_output_tokens: int | None = None,
    ):
        self.provider_id = provider_id or settings.LLM_PROVIDER
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.enable_thinking = (
            enable_thinking if enable_thinking is not None else settings.LLM_ENABLE_THINKING
        )
        # 可选:自定义 baseUrl 覆盖 catalog 预设(留空则用 catalog 的 baseUrl)
        self.base_url_override = base_url
        # 可选:用户显式设置的单次输出上限(None 时构造完 model_meta 后按 catalog 解析)
        self._explicit_output_limit = max_output_tokens

        if not self.api_key:
            raise ValueError("未配置 LLM_API_KEY,请在 .env 中设置或在前端模型设置中配置")

        self.provider = find_provider(self.provider_id)
        if not self.provider:
            raise ValueError(f"未知 provider: {self.provider_id},请检查 models_catalog.json")

        self.model_meta = find_model_meta(self.provider, self.model)
        # 豆包用 Endpoint ID,model_meta 可能匹配不上,用 fallbackThinking 兜底
        # 此时 thinking_mode 取 fallbackThinking

        # 单次输出上限:用户设置 > catalog 模型级 > provider 级 > 系统默认
        self.max_output_tokens = resolve_output_limit(
            self.provider, self.model_meta, self._explicit_output_limit
        )

        # baseUrl 优先级:用户自定义 > catalog 预设
        effective_base_url = self.base_url_override or self.provider["baseUrl"]

        # openai SDK 客户端(指向厂商的 OpenAI 兼容端点)
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=effective_base_url,
        )

    @classmethod
    def from_config_dict(cls, cfg: dict) -> "LLMClient":
        """从单个配置字典构造客户端(列表式配置,阶段 6 重构)

        cfg: 用户保存的某个 LLM 配置项
        { id, name, provider, api_key, model, enable_thinking, base_url }

        失败时(如缺 api_key)抛 ValueError,由调用方决定是否回退到 env 默认。
        """
        api_key = cfg.get("api_key", "")
        if not api_key:
            raise ValueError("配置缺少 api_key")
        return cls(
            provider_id=cfg.get("provider"),
            api_key=api_key,
            model=cfg.get("model"),
            enable_thinking=cfg.get("enable_thinking", True),
            base_url=cfg.get("base_url"),
            max_output_tokens=cfg.get("max_output_tokens"),
        )

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> Generator["StreamChunk", None, None]:
        """流式对话(统一入口,所有 agent 都走这个)

        每次产出 StreamChunk,描述这一片增量:
        - reasoning_delta: 思考链增量(可空,部分模型才有)
        - content_delta: 正式回答增量(可空)
        - tool_call_deltas: 工具调用增量列表(可空,带 index 用于累积)
        - finish_reason: 流结束时为 'tool_calls' / 'stop' / 'length' 等,中途为 None

        工具调用流式累积说明:
        OpenAI 流式响应中,一个 tool_call 会跨多个 chunk:
        - 第一个 chunk: tool_calls[i].id / .type / .function.name
        - 后续 chunk: tool_calls[i].function.arguments 片段
        所以调用方需要按 index 累积 arguments 字符串,完整后才能 json.loads。

        参考:ai-plugin/lib/llm.js 的 _parseSSE

        stop_check:可选的逐 chunk 终止检查(用户终止 agent2 检查等场景)。
            返回 True 时先关掉底层 HTTP 流(否则已建立的流会继续读完),
            再正常结束生成器 —— 调用方拿到的是截至该点的部分输出。
            不传时行为与以前一致。
        """
        extras = build_thinking_extras(
            self.provider, self.model_meta, self.enable_thinking
        ) or {}
        resolved_temp = resolve_temperature(
            self.provider, self.model_meta, self.enable_thinking, temperature
        )

        # 按模型输出能力钳制:超过上限会被厂商拒绝(400)或截断,
        # 统一钳制保证所有调用方(含 agent2 大 JSON 输出)不踩线
        if max_tokens > self.max_output_tokens:
            logger.debug(
                f"max_tokens {max_tokens} 超过模型输出上限,"
                f"钳制为 {self.max_output_tokens}(model={self.model})"
            )
            max_tokens = self.max_output_tokens

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": resolved_temp,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if extras:
            kwargs["extra_body"] = extras

        # 流式调用,SDK 返回迭代器
        # 429 限流在请求发起阶段退避重试(流建立后的中途异常不重试,避免已推送内容重复)
        stream = create_with_rate_limit_retry(
            lambda: self.client.chat.completions.create(**kwargs),
            label=f"chat({self.provider_id}/{self.model})",
        )

        # 流式拆分 <think>...</think>:某些端点把思考内嵌在 content 里,
        # 拆分后标签内 → reasoning,标签外 → content(无标签时原样输出,无副作用)
        splitter = _ThinkTagSplitter()

        for chunk in stream:
            # 终止检查点:逐 chunk 判断,命中即关流退出(未关闭的 OpenAI Stream
            # 会继续读取剩余 token,既不省钱也不省时)
            if stop_check is not None and stop_check():
                _close_stream(stream)
                return
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            # reasoning_content:思考链增量(DeepSeek/Qwen/Kimi 等)
            # OpenAI SDK 1.x+ 把厂商扩展字段放在 delta 的 model_extra 里
            reasoning_delta = ""
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                reasoning_delta = delta.reasoning_content
            else:
                # 兜底:从 model_extra(原始 dict)取
                model_extra = getattr(delta, "model_extra", None) or {}
                if isinstance(model_extra, dict):
                    rc = model_extra.get("reasoning_content")
                    if rc:
                        reasoning_delta = rc

            content_delta = delta.content or ""

            # 流式拆分 <think>...</think>(跨 chunk 标签边界由 splitter 处理)
            split_reasoning = ""
            split_content = ""
            for r, c in splitter.feed(content_delta):
                split_reasoning += r
                split_content += c
            if split_reasoning:
                reasoning_delta = (reasoning_delta or "") + split_reasoning
            content_delta = split_content

            # 工具调用增量(可能同时有多个 tool_call 并行)
            tool_call_deltas: list[ToolCallDelta] = []
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    tool_call_deltas.append(ToolCallDelta(
                        index=tc.index if tc.index is not None else 0,
                        id=tc.id,
                        name=tc.function.name if tc.function and tc.function.name else None,
                        arguments_fragment=tc.function.arguments if tc.function and tc.function.arguments else "",
                    ))

            # finish_reason 时先 flush 拆分器剩余 buffer(未闭合 <think> 等)
            flush_pairs: list[tuple[str, str]] = []
            if choice.finish_reason:
                flush_pairs = splitter.flush()

            # 跳过完全空的 chunk(SSE keep-alive 等;有 flush 内容时不跳过)
            if (
                not reasoning_delta
                and not content_delta
                and not tool_call_deltas
                and not choice.finish_reason
                and not flush_pairs
            ):
                continue

            # 先输出 flush 的内容(在 finish_reason chunk 之前,避免调用方
            # 遇 finish_reason 即 break 时丢失剩余 buffer)
            for r, c in flush_pairs:
                if r or c:
                    yield StreamChunk(reasoning_delta=r, content_delta=c)

            yield StreamChunk(
                reasoning_delta=reasoning_delta,
                content_delta=content_delta,
                tool_call_deltas=tool_call_deltas or None,
                finish_reason=choice.finish_reason,
            )

        # 流自然结束(无 finish_reason 或调用方未 break)兜底 flush
        for r, c in splitter.flush():
            if r or c:
                yield StreamChunk(reasoning_delta=r, content_delta=c)

    def test(self, prompt: str = "你好,请用一句话介绍一下你自己。") -> dict[str, Any]:
        """测试 LLM 连通性并收集完整回复

        返回 { success, message, latency_ms, reply }
        用流式接口调用,收集完整 content 后返回,供前端展示模型实际回复。
        测试时优先关闭深度思考,避免思考链耗时过长(thinking_mode=only 的模型无法关闭)。
        若关闭思考被厂商拒绝(如 kimi-k2.7-code 仅允许 thinking:enabled),
        则按用户原始配置重试一次。

        思考内容处理:
        - reasoning_content / <think> 标签的思考内容在流式过程已通过思考流展示给前端
        - reply 只放正式回复(content),不把思考混入回复
        - 但只要有思考内容就判为成功(模型确实响应了),而非"未响应"
        - 既无回复也无思考才判失败
        """
        import time

        start = time.perf_counter()
        original_thinking = self.enable_thinking
        # 先用关闭思考的快路径;厂商拒绝(400 等 API 异常)且原始配置开着思考时,用原始配置重试
        attempts = [False] + ([original_thinking] if original_thinking else [])
        last_exc: Exception | None = None
        try:
            for thinking_flag in attempts:
                self.enable_thinking = thinking_flag
                try:
                    return self._test_once(prompt, start)
                except openai.RateLimitError:
                    raise  # 429 是配额问题而非配置问题,不换思考配置重试(内部已退避重试过)
                except Exception as e:  # API 层异常(400/401/超时等),尝试下一组配置
                    last_exc = e
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "success": False,
                "message": f"LLM 测试失败: {last_exc}",
                "latency_ms": latency_ms,
                "reply": None,
            }
        finally:
            self.enable_thinking = original_thinking

    def _test_once(self, prompt: str, start: float) -> dict[str, Any]:
        """执行一次测试调用;API 异常向上抛由 test() 决定是否换配置重试"""
        import time

        messages = [{"role": "user", "content": prompt}]
        # 收集完整 content,max_tokens 限制在合理范围(一句口号)
        collected: list[str] = []
        reasoning_collected: list[str] = []
        for chunk in self.chat_stream(messages, max_tokens=128):
            if chunk.content_delta:
                collected.append(chunk.content_delta)
            if chunk.reasoning_delta:
                reasoning_collected.append(chunk.reasoning_delta)
            if chunk.finish_reason in ("stop", "tool_calls", "length"):
                break
        latency_ms = int((time.perf_counter() - start) * 1000)
        reply = "".join(collected).strip()
        reasoning = "".join(reasoning_collected).strip()
        if reply:
            # 有正式回复:正常成功
            return {
                "success": True,
                "message": "LLM 测试成功",
                "latency_ms": latency_ms,
                "reply": reply,
            }
        if reasoning:
            # 无正式回复但有思考:模型确实响应了(思考已在流式过程展示),
            # 判为成功,但 reply 为空,不把思考混入回复。
            return {
                "success": True,
                "message": "LLM 测试成功(模型仅返回思考内容,未给出正式回复)",
                "latency_ms": latency_ms,
                "reply": None,
            }
        # 既无回复也无思考:真正未响应(不属于可重试的 API 异常,直接失败)
        return {
            "success": False,
            "message": "LLM 测试失败:模型未响应(请检查配额或网络)",
            "latency_ms": latency_ms,
            "reply": None,
        }


# ============================================================
# 流式数据结构
# ============================================================


class ToolCallDelta:
    """工具调用增量(对应 OpenAI stream chunk 中的 tool_calls[i])

    一个完整的 tool_call 会跨多个 chunk:
    - 第一个 chunk 带 id 和 name(以及 arguments 起始片段)
    - 后续 chunk 只带 arguments 增量片段
    调用方需要按 index 累积 arguments,完整后才能 json.loads。
    """

    __slots__ = ("index", "id", "name", "arguments_fragment")

    def __init__(
        self,
        index: int,
        id: str | None = None,
        name: str | None = None,
        arguments_fragment: str = "",
    ) -> None:
        self.index = index
        self.id = id  # 仅第一个 chunk 有
        self.name = name  # 仅第一个 chunk 有
        self.arguments_fragment = arguments_fragment  # 多个 chunk 累积

    def __repr__(self) -> str:
        return (
            f"ToolCallDelta(index={self.index}, id={self.id!r}, "
            f"name={self.name!r}, args_len={len(self.arguments_fragment)})"
        )


class _ThinkTagSplitter:
    """流式拆分 <think>...</think> 标签,将标签内文本重定向为 reasoning。

    适用场景:某些模型/端点(DeepSeek-R1 开源版、MiniMax 未配 reasoning_split、
    第三方代理)把思考内嵌在 content 里用 <think> 标签包裹,而非走
    reasoning_content 字段。拆分后标签内 → reasoning_delta,标签外 → content_delta,
    让思考链正确走思考流而非混入正式回复。content 中无 <think> 时原样输出,无副作用。

    跨 chunk 边界处理:当 buffer 尾部可能是标签前缀时(如 "<thi" 是 "<think>" 的前缀),
    保留不输出,等后续 chunk 拼接确认。未闭合的 <think> 在 flush 时按当前状态输出。
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._in_thinking = False
        self._buffer = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        """喂入增量文本,返回 [(reasoning_delta, content_delta), ...] 列表。

        一段文本内可能多次开关标签,故可能产出多对增量。
        返回空列表表示整个 buffer 都是未确定的标签前缀,调用方应等待后续输入。
        """
        if not text:
            return []
        self._buffer += text
        out: list[tuple[str, str]] = []
        while self._buffer:
            tag = self._CLOSE if self._in_thinking else self._OPEN
            idx = self._buffer.find(tag)
            if idx == -1:
                # 未找到完整标签:输出肯定安全的前缀,保留可能是标签前缀的尾部
                safe = self._safe_prefix(self._buffer, tag)
                if safe:
                    out.append((safe, "") if self._in_thinking else ("", safe))
                    self._buffer = self._buffer[len(safe):]
                    if not self._buffer:
                        break
                else:
                    # 整个 buffer 都是标签前缀,等待更多输入
                    break
            else:
                # 找到标签:输出标签前的内容,跳过标签,切换状态
                if idx > 0:
                    out.append((self._buffer[:idx], "") if self._in_thinking else ("", self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(tag):]
                self._in_thinking = not self._in_thinking
        return out

    def flush(self) -> list[tuple[str, str]]:
        """流结束时调用,输出剩余 buffer(未闭合 <think> 按当前状态输出)。"""
        if not self._buffer:
            return []
        out = self._buffer
        self._buffer = ""
        return [(out, "")] if self._in_thinking else [("", out)]

    @staticmethod
    def _safe_prefix(buffer: str, tag: str) -> str:
        """返回 buffer 中肯定不属于 tag 前缀的安全部分(保留尾部可能是前缀的部分)。

        例如 buffer="abc<thi", tag="<think>" → 返回 "abc",保留 "<thi" 等待确认。
        """
        max_overlap = min(len(buffer), len(tag) - 1)
        for i in range(max_overlap, 0, -1):
            if buffer[-i:] == tag[:i]:
                return buffer[:-i]
        return buffer


class StreamChunk:
    """流式响应的单个 chunk

    三个 delta 字段互斥(同一 chunk 通常只会有其中一个有值):
    - reasoning_delta: 思考链增量(reasoning_content)
    - content_delta: 正式回答增量(content)
    - tool_call_deltas: 工具调用增量列表(tool_calls[i])

    finish_reason 在流结束时非空('stop' / 'tool_calls' / 'length' 等)
    """

    __slots__ = ("reasoning_delta", "content_delta", "tool_call_deltas", "finish_reason")

    def __init__(
        self,
        *,
        reasoning_delta: str = "",
        content_delta: str = "",
        tool_call_deltas: list[ToolCallDelta] | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.reasoning_delta = reasoning_delta
        self.content_delta = content_delta
        self.tool_call_deltas = tool_call_deltas
        self.finish_reason = finish_reason

    def __repr__(self) -> str:
        parts = []
        if self.reasoning_delta:
            parts.append(f"reasoning({len(self.reasoning_delta)})")
        if self.content_delta:
            parts.append(f"content({len(self.content_delta)})")
        if self.tool_call_deltas:
            names = [tc.name or '?' for tc in self.tool_call_deltas]
            parts.append(f"tool_call({','.join(names)})")
        if self.finish_reason:
            parts.append(f"finish={self.finish_reason}")
        return f"StreamChunk({', '.join(parts) or 'empty'})"
