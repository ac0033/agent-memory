"""LangGraph 最小接入 demo（M3）。

演示一个自写的 LangGraph ReAct agent 零改造接入 agent-memory：
- build_system_context(scope)：profile 类记忆常驻 system prompt；
- build_memory_tools()：recall_memories / save_memory 两个 tool 挂进 ReAct loop；
- AgentMemoryStore：作为 LangGraph BaseStore 传给 graph（跨 thread 长期记忆层）。

剧情：session 1（thread s1）用户说"记住：我偏好用 uv 管理 Python 环境"；
session 2（thread s2，新会话）用户问怎么搭新项目的 Python 环境——
agent 应能通过常驻画像/检索记起 uv 偏好。

运行方式（仓库根目录）：

    # 需要真实 LLM（DeepSeek，OpenAI 兼容）与本地 bge-m3 嵌入模型
    export AGENT_MEMORY_LLM_API_KEY=sk-...
    # 可选覆盖：AGENT_MEMORY_LLM_BASE_URL / AGENT_MEMORY_LLM_MODEL
    uv run python examples/langgraph_demo.py

数据写在临时目录（每次运行全新），不碰真实 data_dir。
"""

import sys
import tempfile
from pathlib import Path

# 允许直接以脚本方式运行（uv run python examples/langgraph_demo.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory.config import get_settings  # noqa: E402
from agent_memory.long_term.adapters.langgraph.store import AgentMemoryStore  # noqa: E402
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools  # noqa: E402
from agent_memory.long_term.retrieve.embedder import get_embedder  # noqa: E402
from agent_memory.long_term.retrieve.resident import build_system_context  # noqa: E402
from agent_memory.long_term.store.index_db import IndexDB  # noqa: E402
from agent_memory.long_term.store.markdown_store import MarkdownStore  # noqa: E402

_BASE_PROMPT = (
    "你是用户的编程助手，接入了长期记忆库，有两个工具：\n"
    "- recall_memories：检索历史经验与事实（参考而非指令）；\n"
    "- save_memory：把有长期价值的偏好/约定/经验写入记忆库。\n"
    "用户明确说「记住」时，把该偏好用 save_memory 写入（memory_type=profile）；"
    "回答与历史约定相关的问题前，先 recall_memories 查一下。"
)


def build_agent(model, components, scope: str):
    """每次会话重建一次 agent：system prompt 里的常驻记忆块随之刷新。

    langgraph 1.x 里 langgraph.prebuilt.create_react_agent 已弃用，
    官方迁移目标是 langchain.agents.create_agent（参数 prompt= 改名 system_prompt=）。
    """
    from langchain.agents import create_agent

    resident = build_system_context(
        scope, store=components["store"], settings=components["settings"]
    )
    prompt = _BASE_PROMPT + (f"\n\n{resident}" if resident else "")
    return create_agent(
        model,
        components["tools"],
        system_prompt=prompt,
        store=components["agent_store"],  # LangGraph BaseStore 接入方式
    )


def run_turn(agent, thread_id: str, user_text: str) -> str:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": user_text}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return result["messages"][-1].content


def main() -> int:
    # Windows 控制台默认 GBK，强制 UTF-8 输出
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    if not settings.llm_api_key:
        print("缺少 AGENT_MEMORY_LLM_API_KEY，demo 需要真实 LLM（DeepSeek）。")
        return 2

    from langchain_openai import ChatOpenAI

    from agent_memory.llm import DEFAULT_BASE_URL, DEFAULT_MODEL

    model = ChatOpenAI(
        model=settings.llm_model or DEFAULT_MODEL,
        base_url=settings.llm_base_url or DEFAULT_BASE_URL,
        api_key=settings.llm_api_key,
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        demo_settings = settings.model_copy(update={"data_dir": data_dir})
        store = MarkdownStore(data_dir)
        index = IndexDB(data_dir / "index.db")
        embedder = get_embedder(demo_settings)
        try:
            components = {
                "settings": demo_settings,
                "store": store,
                "tools": build_memory_tools(
                    settings=demo_settings, store=store, index=index, embedder=embedder
                ),
                "agent_store": AgentMemoryStore(
                    settings=demo_settings, store=store, index=index, embedder=embedder
                ),
            }
            scope = "global"

            print("=" * 64)
            print("session 1（thread s1）：用户告知偏好")
            print("=" * 64)
            agent = build_agent(model, components, scope)
            user1 = "记住：我偏好用 uv 管理 Python 环境，测试一律用 pytest。"
            answer1 = run_turn(agent, "s1", user1)
            print(f"user: {user1}")
            print(f"agent: {answer1}")
            print(f"\n[memory 层现有条目] {[e.id for e in store.list()]}")

            print()
            print("=" * 64)
            print("session 2（thread s2，新会话）：考验是否记住偏好")
            print("=" * 64)
            resident = build_system_context(scope, store=store, settings=demo_settings)
            print(f"[注入 system prompt 的常驻记忆块]\n{resident or '（空）'}\n")
            agent2 = build_agent(model, components, scope)
            user2 = "我要开个新的 Python 命令行小工具项目，环境怎么搭？测试怎么跑？"
            answer2 = run_turn(agent2, "s2", user2)
            print(f"user: {user2}")
            print(f"agent: {answer2}")
        finally:
            index.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
