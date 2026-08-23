"""演化层（M4a 已实现）：睡眠学习循环——触发 → 定向 → 整合 → 验证 → 修剪。

trigger / consolidate / verify / apply / cycle 五个模块，整理产出是提案
（写 data/review_queue/evolution/），三档验证全过才允许晋升，晋升前快照、
应用后审计（data/logs/evolution_audit.jsonl），可 rollback 回滚。
"""
