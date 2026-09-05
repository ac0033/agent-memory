"""LangGraph 适配器（M3，M7 对齐全量能力）。

- store.AgentMemoryStore：LangGraph BaseStore 实现，namespace ("memories", <scope>)；
- tools.build_memory_tools：与 MCP 全量对齐的 16 个 ReAct tool（三层记忆，
  薄包装 MemoryService；默认完整管线，LLM 缺失显式降级）。
"""
