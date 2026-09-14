"""把 MemCompass 全部用例注入核验台模板，生成可发布的单文件页面。

用法：python build_review.py [--flags flags.json] [--out index.html]
flags.json（可选）：{"<用例 id>": ["自动体检提示", ...]}，来自评测运行的用例健康检查。
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "runners"))
from mc_common import SUBSET_ALIAS, load_items  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--flags", type=Path, default=None)
ap.add_argument("--out", type=Path, default=HERE / "index.html")
args = ap.parse_args()

items = load_items(list(SUBSET_ALIAS))
for x in items:
    x["meta"] = {k: v for k, v in (x.get("meta") or {}).items() if k not in ("canary", "license", "suite_version")}
    x.pop("schema", None)
flags = json.loads(args.flags.read_text(encoding="utf-8")) if args.flags and args.flags.exists() else {}
data = {"built": dt.date.today().isoformat(), "items": items, "flags": flags}
blob = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
html = (HERE / "template.html").read_text(encoding="utf-8").replace("__DATA__", blob)
args.out.write_text(html, encoding="utf-8")
print(f"{len(items)} 条用例，{len(flags)} 条自动标记 → {args.out}（{len(html.encode('utf-8')) / 1024:.0f} KB）")

