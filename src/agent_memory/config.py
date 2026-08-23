"""单一配置模块。

所有配置项都有默认值，可用 AGENT_MEMORY_* 环境变量覆盖。
配置非法时直接抛出 pydantic ValidationError（fail-closed），不做静默降级。
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

_ENV_PREFIX = "AGENT_MEMORY_"


class Settings(BaseModel):
    """agent-memory 运行时配置。

    data_dir 之外的 LLM 相关配置（llm_* / judge_llm_*）在 M0 只定义、不使用：
    llm_* 供 M2 蒸馏使用，judge_llm_* 供评估评委使用（可与蒸馏模型不同家族，异源互审）。
    """

    # 数据目录：memory Markdown 层 / raw 证据层 / index.db 都放在这里
    data_dir: Path = Path("~/.agent-memory/data").expanduser()

    # 嵌入与重排
    embedding_model: str = "BAAI/bge-m3"
    rerank_enabled: bool = False  # 消融开关：默认关闭，评估时对比开启效果
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # OpenAI 兼容端点：蒸馏（M2）用
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

    # OpenAI 兼容端点：评估评委用
    judge_llm_base_url: str | None = None
    judge_llm_api_key: str | None = None
    judge_llm_model: str | None = None

    # 检索与治理
    recall_budget_chars: int = Field(default=2000, gt=0)  # 单次注入上下文的记忆字符预算
    stale_days: int = Field(default=90, ge=0)  # 超过该天数未核实的记忆标记为 stale

    # 整理循环（M4a evolve/）：触发阈值与整理参数，发布门槛的一部分，禁止 agent 自修改
    evolve_interval_days: int = Field(default=7, ge=0)  # 距上次整理超过该天数即触发
    evolve_new_entries_threshold: int = Field(default=50, ge=0)  # 新增条目数触发阈值
    evolve_review_backlog_threshold: int = Field(default=10, ge=0)  # review_queue 积压触发阈值
    evolve_merge_max_distance: float = Field(default=0.25, ge=0.0, le=2.0)  # 去重合并的稠密距离阈值
    evolve_stale_sample_size: int = Field(default=10, gt=0)  # 离线复核抽查的最旧条目数
    evolve_retention_query_count: int = Field(default=10, gt=0)  # retention 档的基准 query 数

    # 人工复核交互（M5）
    # 读取复核门：复核队列有积压时 memory_search 的行为——
    # off 不拦；ask 拦截并等用户确认（acknowledge_pending=true 放行）；
    # strict 一律拒读直到队列清空（无人值守等无法确认的场景用）
    review_gate: Literal["off", "ask", "strict"] = "ask"
    # 强制记忆更新的对话轮数间隔（hook 计数用）：每 N 轮注入一次蒸馏指令
    review_turn_interval: int = Field(default=3, gt=0)

    # HTTP 常驻服务（M6）：streamable-http 传输的监听地址。
    # 默认只绑回环地址——本机部署天然免鉴权；要开放给局域网再显式改 host 并加认证
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8765, gt=0, le=65535)


_ENV_KEYS: dict[str, str] = {
    "data_dir": "AGENT_MEMORY_DATA_DIR",
    "embedding_model": "AGENT_MEMORY_EMBEDDING_MODEL",
    "rerank_enabled": "AGENT_MEMORY_RERANK_ENABLED",
    "reranker_model": "AGENT_MEMORY_RERANKER_MODEL",
    "llm_base_url": "AGENT_MEMORY_LLM_BASE_URL",
    "llm_api_key": "AGENT_MEMORY_LLM_API_KEY",
    "llm_model": "AGENT_MEMORY_LLM_MODEL",
    "judge_llm_base_url": "AGENT_MEMORY_JUDGE_LLM_BASE_URL",
    "judge_llm_api_key": "AGENT_MEMORY_JUDGE_LLM_API_KEY",
    "judge_llm_model": "AGENT_MEMORY_JUDGE_LLM_MODEL",
    "recall_budget_chars": "AGENT_MEMORY_RECALL_BUDGET_CHARS",
    "stale_days": "AGENT_MEMORY_STALE_DAYS",
    "evolve_interval_days": "AGENT_MEMORY_EVOLVE_INTERVAL_DAYS",
    "evolve_new_entries_threshold": "AGENT_MEMORY_EVOLVE_NEW_ENTRIES_THRESHOLD",
    "evolve_review_backlog_threshold": "AGENT_MEMORY_EVOLVE_REVIEW_BACKLOG_THRESHOLD",
    "evolve_merge_max_distance": "AGENT_MEMORY_EVOLVE_MERGE_MAX_DISTANCE",
    "evolve_stale_sample_size": "AGENT_MEMORY_EVOLVE_STALE_SAMPLE_SIZE",
    "evolve_retention_query_count": "AGENT_MEMORY_EVOLVE_RETENTION_QUERY_COUNT",
    "review_gate": "AGENT_MEMORY_REVIEW_GATE",
    "review_turn_interval": "AGENT_MEMORY_REVIEW_TURN_INTERVAL",
    "http_host": "AGENT_MEMORY_HTTP_HOST",
    "http_port": "AGENT_MEMORY_HTTP_PORT",
}


def get_settings(env: dict[str, str] | None = None) -> Settings:
    """从环境变量构建 Settings。

    env 参数仅供测试注入；默认读 os.environ。
    任何字段非法（如 recall_budget_chars 不是正整数）都会抛 ValidationError。
    """
    source = os.environ if env is None else env
    overrides: dict[str, str] = {}
    for field_name, env_key in _ENV_KEYS.items():
        value = source.get(env_key)
        if value is not None:
            overrides[field_name] = value
    return Settings.model_validate(overrides)
