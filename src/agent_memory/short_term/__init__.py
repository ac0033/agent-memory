"""短期记忆层（M7b）：会话日志 transcript 适配层（adapter.py）。

短期记忆 = 当前会话的完整对话记录，载体是 agent 运行时的原生日志，不新建
文件；适配层把机器格式日志解析成干净轮次序列（list[Turn]）。session_end
（会话结束时的蒸馏供料）在 M7b 第二半实现。
"""
