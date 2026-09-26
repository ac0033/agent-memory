"""浮现一致性（框架 §8a，K8/K13 系统层）：同一输入不走缓存重复 N 次，记忆副手的决定是否一致。

评测缓存会把单次采样固化成定值，版本对比时把采样噪声读成退步或进步；这里对每个用例的每一轮
（主动联想子集按轮次、同一份预置记忆）调 N 次 surface，统计：
- 全一致率：N 次决定（浮现 / 沉默）完全相同的轮次占比；
- 多数一致度：每轮与多数决定相同的次数 / N，取平均。
正例轮（金标该浮现）与负例轮分开报告。只测决定本身，不调评委。

用法（只读，不改记忆层之外的任何东西）：
  uv run python docs/research/benchmark-suite/runners/surface_consistency.py \
      --am-root . --splits dev --n 5 --env-file <你的 .env> --out data/logs/memcompass/cons-dev.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "build"))

from mc_common import OUT_ROOT, CachedEmbedder, load_items, read_env_file  # noqa: E402
from mc_run import item_turns  # noqa: E402
from systems import AgentMemorySystem, load_agent_memory, now_date  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--am-root", default=".")
    ap.add_argument("--am-label", default="am_cons")
    ap.add_argument("--splits", default="dev")
    ap.add_argument("--ids", default="")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--temperature", type=float, default=None, help="副手采样温度；不给用服务商默认值")
    args = ap.parse_args()

    am_root = Path(args.am_root).resolve()
    load_agent_memory(am_root)
    from mc_common import DEFAULTS

    from agent_memory.config import get_settings
    from agent_memory.llm import OpenAILLMClient
    from agent_memory.long_term.retrieve.embedder import get_embedder

    env = read_env_file(args.env_file)
    sysd = DEFAULTS["system"]
    settings = get_settings().model_copy(update={
        "llm_api_key": env.get(sysd["key_env"]),
        "llm_base_url": sysd.get("base_url") or env.get(sysd.get("base_url_env", "")),
        "llm_model": sysd["model"],
        "llm_temperature": args.temperature,
    })
    system_llm = OpenAILLMClient.from_settings(settings, cache_dir=None)  # 不走缓存：每次都是新采样
    embedder = CachedEmbedder(get_embedder(settings), OUT_ROOT / "embedding_cache.pkl")

    ids = {x for x in args.ids.split(",") if x} or None
    items = load_items(["mc-proactive-recall"], set(args.splits.split(",")), ids)
    rows = []
    for item in items:
        system = AgentMemorySystem(am_root, args.am_label, embedder, settings, system_llm)
        system.setup(item, "preloaded")
        turns, date = item_turns(item), now_date(item)
        labels = [((p.get("gold") or {}).get("labels") or {}) for p in item["probes"]]
        for i in range(len(turns)):
            lab = labels[min(i, len(labels) - 1)] if labels else {}
            decisions = [bool(system.surface(turns[: i + 1], date).strip()) for _ in range(args.n)]
            majority = max(decisions.count(True), decisions.count(False))
            rows.append({
                "item_id": item["id"], "turn": i + 1, "should_surface": bool(lab.get("should_surface")),
                "decisions": decisions, "consistent": majority == args.n, "agreement": majority / args.n,
            })
            print(item["id"], i + 1, decisions, flush=True)
        system.close()

    def summary(rs):
        if not rs:
            return None
        return {"n_turns": len(rs), "fully_consistent": sum(r["consistent"] for r in rs) / len(rs),
                "mean_agreement": sum(r["agreement"] for r in rs) / len(rs)}

    out = {"n_samples": args.n, "temperature": args.temperature, "all": summary(rows),
           "positive": summary([r for r in rows if r["should_surface"]]),
           "negative": summary([r for r in rows if not r["should_surface"]]), "rows": rows}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
