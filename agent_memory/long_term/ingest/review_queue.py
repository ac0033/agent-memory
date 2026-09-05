"""复核队列（data/review_queue/）：distill / gate / reconcile / 传播共用。

反"丢弃式防御"的落点：管线里任何"机器判不了但可能有价值"的条目都写到
这里，绝不静默丢弃。

文件名用内容哈希而非时间戳（借鉴幂等键思路）：同一条目同一内容重复排队
时覆盖同一文件，重试/重跑不会在队列里堆积重复待办；排队时间记录在
payload 的 queued_at 里，不丢时间信息。

读侧（M5 人工复核交互）：list_review_queue / load_review_item /
delete_review_item 供 MCP 的 memory_review_list / memory_review_resolve
与复核门使用。只认 review_queue 直接子级的 *.yaml（evolution/ 子目录的
整理提案走 evolve 自己的流程，不在这里列出）。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

from agent_memory.io_utils import atomic_write_text, interprocess_lock
from agent_memory.models import MemoryEntry, normalize_entry_id


def _queue_filename(stem: str, payload: dict) -> str:
    """复核队列文件名：<安全 stem>-<内容 hash 前 10 位>.yaml。"""
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:10]
    safe_stem = normalize_entry_id(stem) or "unnamed"
    return f"{safe_stem}-{digest}.yaml"


def _write_queue_payloads(payloads: list[dict], stems: list[str], data_dir: Path) -> list[Path]:
    queue_dir = Path(data_dir) / "review_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    files = []
    with interprocess_lock(Path(data_dir) / "state" / "review_queue.lock"):
        for stem, payload in zip(stems, payloads, strict=True):
            # 文件名哈希只覆盖稳定内容（不含 queued_at），保证重复排队覆盖同一文件
            path = queue_dir / _queue_filename(stem, payload)
            payload = {**payload, "queued_at": datetime.now().isoformat(timespec="seconds")}
            atomic_write_text(
                path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
            )
            files.append(path)
    return files


def write_review_queue(
    entries: list[MemoryEntry], data_dir: Path, reason: str
) -> list[Path]:
    """把待复核条目写入 <data_dir>/review_queue/，返回文件列表。"""
    payloads = [{"reason": reason, "entry": entry.model_dump(mode="json")} for entry in entries]
    return _write_queue_payloads(payloads, [e.id for e in entries], Path(data_dir))


def write_review_queue_raw(
    records: list[tuple[object, str]], data_dir: Path, reason: str
) -> list[Path]:
    """把无法构造为 MemoryEntry 的原始记录写入复核队列（distill 的非法产出等）。

    records: (已由上游脱敏的原始记录, 逐条原因)。复核队列保留脱敏后的结构化
    现场供人工判断，绝不保存凭据原文。
    """
    payloads = [
        {"reason": f"{reason}：{item_reason}", "raw_record": raw}
        for raw, item_reason in records
    ]
    stems = [
        str(raw.get("id", "")) if isinstance(raw, dict) else type(raw).__name__
        for raw, _ in records
    ]
    return _write_queue_payloads(payloads, stems, Path(data_dir))


# ---------------------------------------------------------------- 读侧（M5）


def list_review_queue(data_dir: Path) -> list[dict]:
    """列出复核队列直接子级的全部待办（不含 evolution/ 子目录），按排队时间升序。

    每项含 file / reason / queued_at / entry（可入库的条目 dict）/ raw_record
    （无法构造条目的原始记录）。损坏文件不静默跳过：以 unreadable=True 标记
    留在列表里交人工处置（队列文件的职责就是保留现场）。
    """
    queue_dir = Path(data_dir) / "review_queue"
    items = []
    if not queue_dir.is_dir():
        return items
    for path in sorted(queue_dir.glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("队列文件不是 YAML 映射")
            items.append(
                {
                    "file": path.name,
                    "reason": str(payload.get("reason", "")),
                    "queued_at": str(payload.get("queued_at", "")),
                    "entry": payload.get("entry"),
                    "raw_record": payload.get("raw_record"),
                    "unreadable": False,
                }
            )
        except (yaml.YAMLError, ValueError, OSError) as e:
            items.append(
                {
                    "file": path.name,
                    "reason": "",
                    "queued_at": "",
                    "entry": None,
                    "raw_record": None,
                    "unreadable": True,
                    "error": str(e),
                }
            )
    items.sort(key=lambda it: (it["unreadable"], it["queued_at"]))
    return items


def _resolve_queue_path(data_dir: Path, file_name: str) -> Path:
    """把队列文件名解析成队列目录内的路径，越界/不存在 fail-closed。"""
    if Path(file_name).name != file_name or not file_name.endswith(".yaml"):
        raise ValueError(f"非法队列文件名（只认 review_queue 下的 *.yaml 文件名）: {file_name!r}")
    path = Path(data_dir) / "review_queue" / file_name
    if not path.is_file():
        raise FileNotFoundError(f"复核队列里不存在该待办: {file_name}")
    return path


def load_review_item(data_dir: Path, file_name: str) -> dict:
    """读取一个队列文件的 payload；文件损坏抛错（fail-closed，不替人猜内容）。"""
    path = _resolve_queue_path(data_dir, file_name)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"队列文件不是 YAML 映射: {file_name}")
    return payload


def delete_review_item(data_dir: Path, file_name: str) -> None:
    """删除一个已处理完毕的队列文件。"""
    with interprocess_lock(Path(data_dir) / "state" / "review_queue.lock"):
        _resolve_queue_path(data_dir, file_name).unlink()


def review_queue_lock(data_dir: Path):
    """裁决流程使用的跨进程锁，保证同一待办只有一个裁决成功。"""
    return interprocess_lock(Path(data_dir) / "state" / "review_queue.lock")
