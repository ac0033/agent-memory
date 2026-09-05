"""LLM 客户端抽象（M2）：蒸馏 / 对账 / 评估评委统一走 LLMClient 协议。

依赖注入设计：生产环境用 OpenAILLMClient（OpenAI 兼容端点，默认 DeepSeek），
测试注入任何满足 LLMClient 协议的 fake（如返回固定 JSON 的脚本化客户端）。

fail-closed：缺 api_key 构造即报错；JSON 解析失败重试 1 次，仍失败抛 LLMError，
不做静默降级。

磁盘缓存：构造时传 cache_dir 即开启响应缓存（评估重跑提速用）。
key = sha256(model + kind + system + user)，缓存文件同时记录 model 名，
换模型自然不命中；写盘走临时文件 + os.replace，并发安全。
"""

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent_memory.config import Settings

# 默认端点：DeepSeek 官方 OpenAI 兼容 API
# DeepSeek V4 系列仍为 OpenAI Chat Completions 兼容（base_url 不变，
# response_format=json_object 继续支持；旧模型名 deepseek-chat 已弃用，
# 当前模型清单只有 deepseek-v4-pro / deepseek-v4-flash）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"

_JSON_MAX_ATTEMPTS = 2  # 首次 + 重试 1 次


class LLMError(RuntimeError):
    """LLM 调用或响应解析失败（fail-closed，直接抛出）。"""


@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议：生产实现与测试 fake 都满足它。"""

    def complete(self, system: str, user: str) -> str:
        """普通文本补全，返回模型输出文本。"""
        ...

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        """JSON 补全：要求模型输出符合 schema_description 描述的 JSON 对象。

        实现方负责解析与校验（必须是 JSON object），解析失败重试 1 次后报错。
        """
        ...


class OpenAILLMClient:
    """OpenAI 兼容端点的 LLM 客户端（默认 DeepSeek）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        cache_dir: Path | None = None,
        timeout: float = 300.0,
        max_retries: int = 2,
    ):
        if not api_key:
            raise LLMError(
                "缺少 LLM api_key：请设置 AGENT_MEMORY_LLM_API_KEY"
                "（或 llm_api_key 配置项），否则无法调用真实 LLM"
            )
        # lazy import：没装 openai 时仅在本类实例化时报错，不影响 fake 路径
        from openai import OpenAI

        self.base_url = base_url or DEFAULT_BASE_URL
        self.model = model or DEFAULT_MODEL
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        # 显式超时 + 重试上限（默认 300s × 内部重试 2 次）：openai 默认 600s
        # 超时且重试 2 次，单次挂起最坏 30 分钟，会拖死交互式调用与评估并发池。
        # 交互式路径（MCP/HTTP server）可用 AGENT_MEMORY_LLM_TIMEOUT_SECONDS /
        # AGENT_MEMORY_LLM_MAX_RETRIES 收紧最坏耗时
        self._client = OpenAI(
            base_url=self.base_url, api_key=api_key, timeout=timeout, max_retries=max_retries
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, cache_dir: Path | None = None
    ) -> "OpenAILLMClient":
        """从 Settings 构造；llm_* 字段缺省（None）时用模块默认端点/模型，缺 key 报错。"""
        return cls(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            cache_dir=cache_dir,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    # ------------------------------------------------------------ 磁盘缓存

    def _cache_key(self, kind: str, system: str, user: str) -> str:
        """key = sha256(model + kind + system + user)：换模型 / 换提示词自动不命中。"""
        payload = json.dumps(
            {"model": self.model, "kind": kind, "system": system, "user": user},
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_read(self, key: str) -> Any | None:
        if self.cache_dir is None:
            return None
        try:
            record = json.loads((self.cache_dir / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if record.get("model") != self.model:  # 双保险：key 本身已含 model
            return None
        return record["response"]

    def _cache_write(self, key: str, kind: str, system: str, user: str, response: Any) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "model": self.model,
            "kind": kind,
            "system": system,
            "user": user,
            "response": response,
        }
        # 临时文件 + os.replace：并发写同 key 不会留下半截文件
        tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.cache_dir / f"{key}.json")

    def complete(self, system: str, user: str) -> str:
        key = self._cache_key("text", system, user)
        cached = self._cache_read(key)
        if cached is not None:
            return cached
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content
        if not content:
            raise LLMError("LLM 返回了空内容")
        self._cache_write(key, "text", system, user, content)
        return content

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        """JSON 补全：response_format=json_object + 提示词约束，解析失败重试 1 次。"""
        json_system = (
            f"{system}\n\n你必须只输出一个 JSON object（不要输出任何其他文字、不要用"
            f" markdown 代码块包裹），结构要求：\n{schema_description}"
        )
        key = self._cache_key("json", json_system, user)
        cached = self._cache_read(key)
        if cached is not None:
            return cached
        last_error: Exception | None = None
        for attempt in range(1, _JSON_MAX_ATTEMPTS + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": json_system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content or ""
                parsed: Any = json.loads(content)
                if not isinstance(parsed, dict):
                    raise LLMError(f"LLM 输出的 JSON 不是 object（第 {attempt} 次）: {content!r}")
                self._cache_write(key, "json", json_system, user, parsed)
                return parsed
            except (json.JSONDecodeError, LLMError) as e:
                last_error = e
        raise LLMError(
            f"LLM JSON 输出解析失败，重试 {_JSON_MAX_ATTEMPTS} 次后放弃: {last_error}"
        ) from last_error
