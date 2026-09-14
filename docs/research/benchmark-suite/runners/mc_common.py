"""MemCompass runner 公共模块（v0.2 草稿；属于评测执行器，用户审核后迁入 evals/runners/）。

- 数据加载：examples.yaml + migrated.yaml + generated.yaml；按子集 / 切分 / id 过滤；
- LLM 客户端：答题器（固定，默认 DeepSeek）与评委（异源，默认 qwen3.8-max，经阿里云 token-plan 端点），
  以及可选的第二评委（glm-5.2，用于评委一致性）；密钥只从 --env-file 读取，全程不打印；
- 并发与原生调用锁、JSON 调用重试（限流退避）；
- 统计：F0.5、比例的 Wilson 区间、配对 McNemar + bootstrap（复用 evals/runners/metrics.py）。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent  # docs/research/benchmark-suite（草稿）或 evals/memcompass（迁入后的冻结副本）
REPO = next(p for p in HERE.parents if (p / "pyproject.toml").exists())
DATASETS = SUITE / "datasets"
FILES = ("examples.yaml", "migrated.yaml", "generated.yaml")
OUT_ROOT = REPO / "data" / "logs" / "memcompass"
CACHE_DIR = REPO / "data" / "logs" / "llm_cache"
TZ = dt.timezone(dt.timedelta(hours=8))

SUBSET_ALIAS = {
    "pr": "mc-proactive-recall", "ca": "mc-completeness-alignment", "at": "mc-asof-temporal",
    "fg": "mc-forget-request", "mp": "mc-memory-poisoning", "ts": "mc-task-state",
    "pf": "mc-present-fidelity", "xa": "mc-cross-agent",
}

# 默认模型：答题器与被测系统内部 LLM 用 DeepSeek 官方 deepseek-flash（生产默认）；评委与答题器异源
DEFAULTS = {
    # 2026-09-14 下午两个端点先后耗尽（DeepSeek 官方 402、token-plan 周额度），充值 / 重置后恢复原协议：
    # 答题器与被测系统走 DeepSeek 官方，评委走 token-plan——两边分摊额度，评委（qwen）与答题器异源
    "actor": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "key_env": "DEEPSEEK_API_KEY"},
    "system": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "key_env": "DEEPSEEK_API_KEY"},
    "judge": {"base_url_env": "OPENAI_BASE_URL", "model": "qwen3.8-max", "key_env": "DASHSCOPE_API_KEY"},
    "judge2": {"base_url_env": "OPENAI_BASE_URL", "model": "glm-5.2", "key_env": "DASHSCOPE_API_KEY"},
}

native_lock = threading.RLock()  # sqlite-vec / torch 原生调用串行（Windows + 3.14 多线程偶发段错误）
_count_lock = threading.Lock()
llm_calls: dict[str, int] = {}


class CachedEmbedder:
    """按文本 sha256 缓存向量（内存 + 磁盘）。同一文本在不同对照组、模式、种子之间反复出现，
    缓存后只算一次；向量与直接调用完全相同，对被测系统之间的比较没有影响。"""

    def __init__(self, inner, path: Path | None = None):
        import pickle

        self.inner = inner
        self.path = path
        self.cache: dict[str, bytes] = {}
        self._lock = threading.RLock()
        self._dirty = 0
        if path and path.exists():
            try:
                self.cache = pickle.loads(path.read_bytes())
            except Exception:  # noqa: BLE001
                self.cache = {}

    @staticmethod
    def _k(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import array

        keys = [self._k(t) for t in texts]
        with self._lock:
            missing = [(i, t) for i, (t, k) in enumerate(zip(texts, keys)) if k not in self.cache]
        if missing:
            uniq = list(dict.fromkeys(t for _, t in missing))
            with native_lock:
                vecs = self.inner.embed_texts(uniq)
            with self._lock:
                for t, v in zip(uniq, vecs):
                    self.cache[self._k(t)] = array.array("f", v).tobytes()
                self._dirty += len(uniq)
        out = []
        with self._lock:
            for k in keys:
                a = array.array("f")
                a.frombytes(self.cache[k])
                out.append(list(a))
        return out

    def save(self) -> None:
        import pickle

        if self.path and self._dirty:
            with self._lock:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_bytes(pickle.dumps(self.cache))
                os.replace(tmp, self.path)
                self._dirty = 0


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 数据


def load_items(subsets: list[str], splits: set[str] | None = None, ids: set[str] | None = None) -> list[dict]:
    out = []
    for s in subsets:
        name = SUBSET_ALIAS.get(s, s)
        for fname in FILES:
            f = DATASETS / name / fname
            if not f.exists():
                continue
            for x in yaml.safe_load_all(f.read_text(encoding="utf-8")):
                if not x:
                    continue
                if splits and x["split"] not in splits:
                    continue
                if ids and x["id"] not in ids:
                    continue
                out.append(x)
    return out


def parse_time(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, 10, 0, tzinfo=TZ)
    text = str(value)
    if len(text) == 10:
        x = dt.date.fromisoformat(text)
        return dt.datetime(x.year, x.month, x.day, 10, 0, tzinfo=TZ)
    t = dt.datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=TZ)


def session_date(s: dict) -> dt.date:
    return parse_time(s["date"]).date()


def render_session(s: dict) -> str:
    head = f"[{str(s['date'])[:16]} {s['session_id']}" + (f" {s['host']}" if s.get("host") and s["host"] != "generic" else "") \
        + (f" {s['scope']}" if s.get("scope") else "") + "]"
    return head + "\n" + "\n".join(f"  {m['role']}: {m['content'].strip()}" for m in s["messages"])


def render_history(item: dict, upto: dict | None = None) -> str:
    return "\n".join(render_session(s) for s in item["history"]["sessions"])


# ---------------------------------------------------------------- LLM


def read_env_file(path: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path:
        return env
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


class ChatClient:
    """最小 OpenAI 兼容客户端：complete_json + 磁盘缓存 + 限流退避。与 agent_memory.llm 解耦，
    这样被测系统的代码版本（基线 / 优化后）不影响答题器与评委。"""

    def __init__(self, role: str, base_url: str, api_key: str, model: str, cache: bool = True, timeout: float = 180):
        from openai import OpenAI

        self.role = role
        self.base_url = base_url
        self.model = model
        self.cache_dir = CACHE_DIR / "memcompass" if cache else None
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=2)

    def _key(self, system: str, user: str, extra: str) -> str:
        import hashlib

        return hashlib.sha256(json.dumps([self.model, system, user, extra], ensure_ascii=False).encode()).hexdigest()

    def complete_json(self, system: str, user: str, schema: str, seed_tag: str = "") -> dict:
        key = self._key(system, user, schema + seed_tag)
        if self.cache_dir:
            f = self.cache_dir / f"{key}.json"
            if f.exists():
                try:
                    return json.loads(f.read_text(encoding="utf-8"))["response"]
                except (OSError, json.JSONDecodeError, KeyError):
                    pass
        sys_full = f"{system}\n\n只输出一个 JSON object（不要 markdown 代码块），结构：\n{schema}"
        backoff = [5, 15, 40, 90]
        last = None
        for attempt in range(len(backoff) + 1):
            try:
                with _count_lock:
                    llm_calls[self.role] = llm_calls.get(self.role, 0) + 1
                resp = self._client.chat.completions.create(
                    model=self.model, messages=[{"role": "system", "content": sys_full}, {"role": "user", "content": user}],
                    response_format={"type": "json_object"}, temperature=0,
                )
                text = resp.choices[0].message.content or ""
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise ValueError("JSON 不是 object")
                if self.cache_dir:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
                    tmp.write_text(json.dumps({"model": self.model, "response": parsed}, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, self.cache_dir / f"{key}.json")
                return parsed
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).lower()
                transient = "429" in msg or "rate" in msg or "timeout" in msg or "connection" in msg or "502" in msg \
                    or "503" in msg or isinstance(e, (json.JSONDecodeError, ValueError))
                if attempt < len(backoff) and transient:
                    time.sleep(backoff[attempt])
                    continue
                raise
        raise RuntimeError(f"LLM 调用失败：{last}")


def build_client(role: str, env: dict[str, str], override_model: str | None = None, cache: bool = True) -> ChatClient:
    spec = DEFAULTS[role]
    base = spec.get("base_url") or env.get(spec.get("base_url_env", ""), "")
    key = env.get(spec["key_env"]) or os.environ.get(spec["key_env"])
    if not base or not key:
        raise RuntimeError(f"{role} 的端点或密钥缺失（需要 --env-file 中的 {spec['key_env']}"
                           + (f" 与 {spec['base_url_env']}" if spec.get("base_url_env") else "") + "）")
    return ChatClient(role, base, key, override_model or spec["model"], cache=cache)


# ---------------------------------------------------------------- 统计


def f_beta(p: float | None, r: float | None, beta: float = 0.5) -> float | None:
    if p is None or r is None:
        return None
    if p == 0 and r == 0:
        return 0.0
    b2 = beta * beta
    return (1 + b2) * p * r / (b2 * p + r) if (b2 * p + r) else 0.0


def rate(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return (sum(bool(x) for x in xs) / len(xs)) if xs else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def mean(xs) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def paired(a: list[bool], b: list[bool]):
    sys.path.insert(0, str(REPO / "evals" / "runners"))
    import metrics  # noqa: WPS433

    return metrics.paired_stats(a, b)


def fmt(v, pct: bool = False) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.0%}" if pct else f"{v:.2f}"
    return str(v)


def contains_any(text: str, needles: list[str]) -> list[str]:
    text = text or ""
    return [n for n in needles if n and n in text]
