"""stdio 转发层：宿主注册的 MCP 入口（`agent-memory mcp`）。

宿主按会话拉起这个进程。它自己不加载嵌入模型、不碰记忆库：

- 工具清单直接取本地代码里的定义（与后台进程同一份 build_server），握手不用等模型；
- 每次工具调用先确保后台进程在跑且代码是最新的（server/daemon.py），再用一次短连接
  把调用原样转给它，结果原样返回；
- 后台进程恰好空闲退出或被重启导致连接失败时，重新拉起后重试一次。

所以同时开多个会话，本机也只有一个后台进程、一份模型。stdout 只走 JSON-RPC，
提示信息一律写 stderr。
"""

from __future__ import annotations

import sys
import threading
from functools import partial
from typing import Any

import anyio

from agent_memory.config import Settings, get_settings
from agent_memory.server import daemon

# 单次工具调用的读超时：对话蒸馏可能要几分钟，宿主自己的超时另算
_CALL_TIMEOUT_SECONDS = 900.0


async def forward_call(settings: Settings, name: str, arguments: dict[str, Any]):
    from mcp.client.client import Client

    last_error: Exception | None = None
    for attempt in range(2):
        await anyio.to_thread.run_sync(partial(daemon.ensure_running, settings))
        try:
            async with Client(
                daemon.mcp_url(settings), read_timeout_seconds=_CALL_TIMEOUT_SECONDS
            ) as client:
                return await client.call_tool(name, arguments)
        except daemon.DaemonError:
            raise
        except Exception as e:  # 连接层失败：后台进程可能刚退出，重新拉起后再试一次
            last_error = e
            print(
                f"[agent-memory] 转发 {name} 第 {attempt + 1} 次失败：{e!r}",
                file=sys.stderr,
                flush=True,
            )
    raise daemon.DaemonError(
        f"转发到后台进程失败（已重试）：{last_error!r}。"
        f"查看日志 {daemon.log_path(settings)}，或运行 agent-memory daemon status"
    )


def build_proxy(settings: Settings):
    """工具定义取本地代码（service 不会被调用），call_tool 换成转发。"""
    from agent_memory.server.mcp_server import build_server

    server = build_server(None)  # type: ignore[arg-type]

    async def call_tool(name: str, arguments: dict[str, Any], context=None):
        return await forward_call(settings, name, arguments)

    server.call_tool = call_tool  # type: ignore[method-assign]
    return server


def _warm_up(settings: Settings) -> None:
    """会话一开始就在后台拉起（或按新代码重启）后台进程，第一次工具调用少等一会儿。"""
    try:
        daemon.ensure_running(settings)
    except Exception as e:
        print(f"[agent-memory] 预热后台进程失败：{e}", file=sys.stderr, flush=True)


def main(warm: bool = True) -> None:
    import logging

    # 每次转发都会打一行 HTTP 请求日志；宿主会把 MCP 进程的 stderr 收进自己的日志，别刷屏
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = get_settings()
    if warm:
        threading.Thread(target=_warm_up, args=(settings,), daemon=True).start()
    build_proxy(settings).run("stdio")
