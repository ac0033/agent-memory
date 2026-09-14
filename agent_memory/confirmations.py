"""待确认队列（v0.2：P29 复述确认 + 无人值守入队）。

与写入复核队列（review_queue）分开：这里存的是"任务理解需要用户确认"的事项——
无人值守时 agent 不能调 ask_user，把需要确认的部分写进来，其余照常处理；用户上线后逐条裁决。
存储：data/confirmations/<id>.yaml（每项一个文件，id 为内容哈希 + 时间，幂等）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

import yaml

from agent_memory.io_utils import atomic_write_text

_ID_RE = re.compile(r"^cf-[0-9a-f]{12}$")


def _dir(data_dir: Path) -> Path:
    return Path(data_dir) / "confirmations"


def enqueue(
    data_dir: Path,
    message: str,
    scope: str,
    restatement: dict | None = None,
    task: str | None = None,
) -> dict:
    if not message or not message.strip():
        raise ValueError("待确认事项 message 不能为空")
    cid = "cf-" + hashlib.sha256(f"{scope}|{task}|{message}".encode()).hexdigest()[:12]
    item = {
        "id": cid,
        "scope": scope,
        "task": task,
        "message": message.strip(),
        "restatement": restatement or {},
        "status": "pending",
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = _dir(data_dir) / f"{cid}.yaml"
    atomic_write_text(path, yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
    return item


def list_pending(data_dir: Path) -> list[dict]:
    d = _dir(data_dir)
    if not d.is_dir():
        return []
    items = []
    for p in sorted(d.glob("cf-*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("status") == "pending":
            items.append(data)
    return sorted(items, key=lambda x: x.get("queued_at", ""))


def resolve(data_dir: Path, cid: str, decision: str, reply: str | None = None) -> dict:
    if not _ID_RE.fullmatch(cid):
        raise ValueError(f"非法的确认项 id：{cid!r}")
    if decision not in {"approve", "reject", "modify"}:
        raise ValueError("decision 只能是 approve / reject / modify")
    path = _dir(data_dir) / f"{cid}.yaml"
    if not path.exists():
        raise KeyError(cid)
    item = yaml.safe_load(path.read_text(encoding="utf-8"))
    item.update(
        status="resolved",
        decision=decision,
        reply=reply,
        resolved_at=datetime.now().isoformat(timespec="seconds"),
    )
    atomic_write_text(path, yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
    return item
