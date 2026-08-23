"""工作记忆 schema（M7a 核心）。

工作记忆 = 当前任务的持久状态结构（任务目标、已确认决策、变量、TODO、
未决问题），是**操作层**而非知识层，与长期记忆（agent_memory.models.MemoryEntry）
的关键区别：
- 每个 scope 一份文档（不是原子条目集合），写入语义是全量替换；
- 写入只过脱敏（redact），不过评价门、不做对账——评价门的祈使句拦截与
  TODO 天然冲突（"修 bug""跑测试"都是祈使句）；
- 不进向量索引、不进进化循环。

turn_watermark 是"本份工作记忆已更新到第几轮对话"的水位，供调用方做
新鲜度补偿判断（当前轮次超过水位说明工作记忆可能滞后，见 render.is_stale）。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agent_memory.models import is_valid_scope


class TodoItem(BaseModel):
    """一条待办。status 只有 pending / done 两态，完成不删除（留痕供回溯）。"""

    content: str
    status: Literal["pending", "done"] = "pending"

    @field_validator("content")
    @classmethod
    def content_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("todo content 不能为空")
        return v.strip()


class WorkingMemory(BaseModel):
    """一个 scope 的当前任务状态。全量替换语义：没有部分更新。"""

    scope: str  # "global" / "repo:<slug>" / "agent:<name>"，与长期记忆同一取值空间
    goal: str = ""  # 当前任务目标
    decisions: list[str] = Field(default_factory=list)  # 已确认决策
    variables: dict[str, str] = Field(default_factory=dict)  # 任务变量（键值均为文本）
    todos: list[TodoItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # 未决问题 / 阻塞
    # 新鲜度水位：本份状态已更新到第几轮对话；current_turn > watermark 即视为可能滞后
    turn_watermark: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=datetime.now)

    @field_validator("scope")
    @classmethod
    def scope_must_match_pattern(cls, v: str) -> str:
        if not is_valid_scope(v):
            raise ValueError(
                "scope 必须匹配 global | repo:<slug> | agent:<name>，"
                f"slug 为小写字母/数字/连字符，收到: {v!r}"
            )
        return v
