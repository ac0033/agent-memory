"""layer1 召回评估 runner（可信根，agent 不得修改用例内容，只可修 runner 代码）。

对每条用例：把 memories_expected 灌进一个临时目录的记忆层（绝不用真实 data_dir），
建索引后用 question 做混合检索；判定标准：memories_expected 至少一条 id 出现在
top5 即命中。输出每条用例命中情况与总 recall@5。

运行：uv run python evals/runners/recall_eval.py
退出码：recall@5 >= 0.9 为 0，否则为 1。
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

# 允许直接以脚本方式运行（uv run python evals/runners/recall_eval.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_memory.config import Settings  # noqa: E402
from agent_memory.models import MemoryEntry  # noqa: E402
from agent_memory.retrieve.embedder import get_embedder  # noqa: E402
from agent_memory.retrieve.hybrid import HybridSearcher  # noqa: E402
from agent_memory.store.index_db import IndexDB  # noqa: E402
from agent_memory.store.markdown_store import MarkdownStore  # noqa: E402

DATASET_DIR = Path(__file__).resolve().parent.parent / "datasets" / "layer1"
TOP_K = 5
PASS_THRESHOLD = 0.9


def expected_to_entry(mem: dict, case_id: str) -> MemoryEntry:
    """用例里的 memories_expected 只有 id/content/memory_type，其余字段按默认值补全。"""
    today = date.today()
    return MemoryEntry(
        id=mem["id"],
        content=mem["content"],
        memory_type=mem["memory_type"],
        scope="global",
        confidence="high",
        source=f"eval:{case_id}",
        created_at=today,
        last_verified=today,
    )


def run_case(case_file: Path, embedder, settings: Settings) -> tuple[bool, list[str], list[str]]:
    """单条用例：临时目录建库 → 检索 → 判定。返回 (是否命中, top5 ids, expected ids)。"""
    case = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = MarkdownStore(tmp_path)
        index = IndexDB(tmp_path / "index.db")
        try:
            entries = [expected_to_entry(m, case["id"]) for m in case["memories_expected"]]
            vectors = embedder.embed_texts([e.content for e in entries])
            for entry, vector in zip(entries, vectors, strict=True):
                store.create(entry)
                index.upsert(entry, vector)
            searcher = HybridSearcher(store, index, embedder, settings)
            results = searcher.search(case["question"], scopes=["global"], k=TOP_K)
        finally:
            index.close()
    top_ids = [r.entry.id for r in results]
    expected_ids = [m["id"] for m in case["memories_expected"]]
    hit = bool(set(top_ids) & set(expected_ids))
    return hit, top_ids, expected_ids


def main() -> int:
    settings = Settings()
    embedder = get_embedder(settings)
    case_files = sorted(DATASET_DIR.glob("*.yaml"))
    if not case_files:
        print(f"未找到评估用例：{DATASET_DIR}", file=sys.stderr)
        return 2

    hits = 0
    for f in case_files:
        hit, top_ids, expected_ids = run_case(f, embedder, settings)
        hits += int(hit)
        mark = "HIT " if hit else "MISS"
        print(f"[{mark}] {f.name}: expected={expected_ids} top{TOP_K}={top_ids}")

    recall = hits / len(case_files)
    print(f"\nrecall@{TOP_K} = {hits}/{len(case_files)} = {recall:.2%}")
    if recall >= PASS_THRESHOLD:
        print(f"通过（门槛 {PASS_THRESHOLD:.0%}）")
        return 0
    print(f"未通过（门槛 {PASS_THRESHOLD:.0%}）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
