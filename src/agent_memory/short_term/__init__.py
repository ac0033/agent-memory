"""短期记忆层（M7b）：会话日志 transcript 适配层（adapter.py）。

短期记忆 = 当前会话的完整对话记录，载体是 agent 运行时的原生日志，不新建
文件；适配层把机器格式日志解析成干净轮次序列（list[Turn]）。会话结束的
收尾编排（归档原文 + 联合蒸馏 + 已完成 TODO 清理）在 server 侧的
MemoryService.session_end 实现（mcp_server.py，M7b 第二半）。
"""
