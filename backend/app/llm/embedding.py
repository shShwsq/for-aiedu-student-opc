"""Embedding 客户端

用途:测试用户配置的 Embedding 服务连通性,返回向量维度。

支持两种后端(由 models_catalog.json 的 provider.backend 决定,缺失时按 provider_id 启发式判断):
1. DashScope 原生:
   - 纯文本:POST /services/embeddings/text-embedding/text-embedding
     body: { model, input: { texts: [text] }, parameters: { text_type: 'document' } }
   - 多模态:POST /services/embeddings/multimodal-embedding/multimodal-embedding
     body: { model, input: { contents: [{ text }] } }
2. OpenAI 兼容(智谱/百度/火山/Jina 等):
   - 纯文本:POST /embeddings
     body: { model, input: text, dimensions?: 1024 }
   - 多模态(豆包 vision):POST /embeddings/multimodal
     body: { model, input: [{ type: 'text', text }], encoding_format: 'float', dimensions?: 1024 }
   - 部分模型通过 dimensions 参数指定输出维度
   - OpenAI 兼容路径会校验返回向量维度与 expected_dimension 一致,不一致抛异常
"""
import time
from typing import Any

import httpx

from app.llm.client import _load_catalog, find_provider


def find_embedding_provider(provider_id: str) -> dict[str, Any] | None:
    """按 id 查找 embedding 厂商元信息"""
    catalog = _load_catalog()
    for p in catalog.get("embeddingProviders", []):
        if p["id"] == provider_id:
            return p
    return None


def find_embedding_model_meta(
    provider: dict[str, Any], model_id: str
) -> dict[str, Any] | None:
    """在 embedding provider 内查找模型元信息(维度/多模态/dimensionsParam)"""
    for m in provider.get("models", []):
        if m["id"] == model_id:
            return m
    return None


class EmbeddingClient:
    """Embedding 测试客户端

    阶段 6:仅用于连通性测试,尚未接入向量检索流程
    """

    def __init__(
        self,
        provider_id: str,
        api_key: str,
        model: str,
        base_url: str | None = None,
    ):
        if not api_key:
            raise ValueError("未配置 Embedding API Key")

        self.provider_id = provider_id
        self.api_key = api_key
        self.model = model

        self.provider = find_embedding_provider(provider_id)
        if not self.provider:
            raise ValueError(
                f"未知 embedding provider: {provider_id},请检查 models_catalog.json"
            )

        # baseUrl 优先级:用户自定义 > catalog 预设
        self.base_url = (base_url or self.provider.get("baseUrl", "")).rstrip("/")

        self.model_meta = find_embedding_model_meta(self.provider, model)
        # 多模态/维度参数:模型级 fallback 到 provider 级
        self.multimodal = bool(
            (self.model_meta or {}).get("multimodal")
            or self.provider.get("fallbackMultimodal")
        )
        # OpenAI 兼容厂商是否走 /embeddings/multimodal 端点(豆包 vision)
        self.multimodal_endpoint = bool(
            (self.model_meta or {}).get("multimodalEndpoint")
            or self.provider.get("fallbackMultimodalEndpoint")
        )
        self.dimensions_param = bool(
            (self.model_meta or {}).get("dimensionsParam")
            or self.provider.get("fallbackDimensionsParam")
        )
        self.expected_dimension = (
            (self.model_meta or {}).get("dimension")
            or self.provider.get("fallbackDimension")
            or 1024
        )

    def embed(self, text: str) -> list[float]:
        """生成单条文本的向量(用于测试)

        返回向量(list[float])。失败抛异常。

        分派逻辑:
        - backend 优先用 catalog 显式声明,缺失时按 provider_id + baseUrl 启发式判断
          (兼容旧 catalog 未声明 backend 的情况)
        - DashScope 原生:多模态模型走 multimodal-embedding 端点,纯文本走 text-embedding 端点
        - OpenAI 兼容:豆包 vision 走 /embeddings/multimodal,其余走 /embeddings
        """
        backend = self.provider.get("backend")
        if not backend:
            # 兼容旧 catalog:DashScope 原生 baseUrl 不含 /compatible-mode
            if self.provider_id == "dashscope" and "/compatible-mode" not in self.base_url:
                backend = "dashscope"
            else:
                backend = "openai"

        if backend == "dashscope":
            # DashScope 原生 API:多模态走独立端点
            if self.multimodal:
                return self._embed_dashscope_multimodal(text)
            return self._embed_dashscope_text(text)

        # OpenAI 兼容厂商:豆包 vision 走 /embeddings/multimodal,其余走 /embeddings
        if self.multimodal_endpoint:
            return self._embed_openai_multimodal(text)
        return self._embed_openai_text(text)

    def test(self, text: str = "你好,这是一个连通性测试") -> dict[str, Any]:
        """测试连通性

        返回 { success, message, latency_ms, dimension }
        """
        start = time.perf_counter()
        try:
            vec = self.embed(text)
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "success": True,
                "message": "Embedding 测试成功",
                "latency_ms": latency_ms,
                "dimension": len(vec),
            }
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "success": False,
                "message": f"Embedding 测试失败: {e}",
                "latency_ms": latency_ms,
                "dimension": None,
            }

    # ============================================================
    # DashScope 原生 API
    # ============================================================

    def _embed_dashscope_text(self, text: str) -> list[float]:
        """DashScope 原生 text-embedding 端点(纯文本)

        POST {base_url}/services/embeddings/text-embedding/text-embedding
        body: { model, input: { texts: [text] }, parameters: { text_type: 'document' } }
        """
        url = f"{self.base_url}/services/embeddings/text-embedding/text-embedding"
        body = {
            "model": self.model,
            "input": {"texts": [text]},
            "parameters": {"text_type": "document"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        embeddings = (
            data.get("output", {}).get("embeddings", []) if isinstance(data, dict) else []
        )
        if not embeddings or "embedding" not in embeddings[0]:
            raise ValueError(f"响应缺少 embedding 字段: {str(data)[:200]}")
        return embeddings[0]["embedding"]

    def _embed_dashscope_multimodal(self, text: str) -> list[float]:
        """DashScope 原生 multimodal-embedding 端点(qwen3-vl-embedding / tongyi-embedding-vision-plus)

        POST {base_url}/services/embeddings/multimodal-embedding/multimodal-embedding
        body: { model, input: { contents: [{ text }] }, parameters?: { dimension: 1024 } }

        注意:DashScope 的维度参数是 `dimension`(单数),放在 `parameters` 对象里,
        与 OpenAI 兼容路径的 `dimensions`(复数,顶层)不同。
        各模型默认维度不同(qwen3-vl-embedding=2560, tongyi-...-plus=1152),
        不传 dimension 参数会返回默认维度,与 catalog 标记的 1024 不一致,
        故 dimensionsParam=true 时显式传 parameters.dimension=expected_dimension 强制对齐。
        """
        url = f"{self.base_url}/services/embeddings/multimodal-embedding/multimodal-embedding"
        body: dict[str, Any] = {
            "model": self.model,
            "input": {"contents": [{"text": text}]},
        }
        if self.dimensions_param:
            body["parameters"] = {"dimension": self.expected_dimension}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        embeddings = (
            data.get("output", {}).get("embeddings", []) if isinstance(data, dict) else []
        )
        if not embeddings or "embedding" not in embeddings[0]:
            raise ValueError(f"响应缺少 embedding 字段: {str(data)[:200]}")
        vec = embeddings[0]["embedding"]
        self._check_dimension(vec)
        return vec

    # ============================================================
    # OpenAI 兼容 /embeddings
    # ============================================================

    def _embed_openai_text(self, text: str) -> list[float]:
        """OpenAI 兼容 /embeddings 端点(纯文本)

        POST {base_url}/embeddings
        body: { model, input: text, dimensions?: 1024 }(部分模型支持 dimensions 参数)
        """
        url = self._build_openai_embeddings_url()

        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
        }
        if self.dimensions_param:
            body["dimensions"] = self.expected_dimension

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        embeddings = data.get("data", []) if isinstance(data, dict) else []
        if not embeddings or "embedding" not in embeddings[0]:
            raise ValueError(f"响应缺少 embedding 字段: {str(data)[:200]}")
        vec = embeddings[0]["embedding"]
        self._check_dimension(vec)
        return vec

    def _embed_openai_multimodal(self, text: str) -> list[float]:
        """OpenAI 兼容 /embeddings/multimodal 端点(豆包 vision)

        POST {base_url}/embeddings/multimodal
        body: { model, input: [{ type: 'text', text }], encoding_format: 'float', dimensions?: 1024 }
        """
        url = self._build_openai_embeddings_url(multimodal=True)

        body: dict[str, Any] = {
            "model": self.model,
            "input": [{"type": "text", "text": text}],
            "encoding_format": "float",
        }
        if self.dimensions_param:
            body["dimensions"] = self.expected_dimension

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # 豆包 multimodal 返回 data.data.embedding(对象),标准 OpenAI 返回 data.data[0].embedding(数组)
        embeddings = data.get("data") if isinstance(data, dict) else None
        vec = None
        if isinstance(embeddings, list) and embeddings and "embedding" in embeddings[0]:
            vec = embeddings[0]["embedding"]
        elif isinstance(embeddings, dict) and "embedding" in embeddings:
            vec = embeddings["embedding"]

        if not vec:
            raise ValueError(f"响应缺少 embedding 字段: {str(data)[:200]}")
        self._check_dimension(vec)
        return vec

    # ============================================================
    # 辅助
    # ============================================================

    def _build_openai_embeddings_url(self, *, multimodal: bool = False) -> str:
        """拼接 OpenAI 兼容端点 URL

        baseUrl 可能已含版本前缀(/v1、/v3、/v4 等),直接拼 /embeddings[/multimodal];
        否则补 /v1(纯 baseUrl 的厂商)。
        """
        import re

        base = (self.base_url or "https://api.openai.com").rstrip("/")
        suffix = "/embeddings/multimodal" if multimodal else "/embeddings"
        if re.search(r"/v\d+$", base):
            return f"{base}{suffix}"
        return f"{base}/v1{suffix}"

    def _check_dimension(self, vec: list[float]) -> None:
        """校验返回向量维度与 expected_dimension 一致(OpenAI 兼容路径)

        维度不匹配会导致向量检索时点积维度不匹配(cosine similarity 算出 NaN),
        故测试阶段即拦截,提示用户检查模型配置或切换模型后清理重建向量库。
        """
        if len(vec) != self.expected_dimension:
            raise ValueError(
                f"维度不匹配: 期望 {self.expected_dimension}, 实际 {len(vec)}(模型: {self.model})"
            )
