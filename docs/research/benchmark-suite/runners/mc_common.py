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
import shutil
import subprocess
import sys
import tempfile
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
    # 2026-09-15：token-plan 周额度再次耗尽，评委改为 Kimi K3（用户的会员，经 Kimi Code CLI 调用，不走 API）。
    # 三方互不相同：答题器 DeepSeek、评委 Kimi、用例修订 Claude。t2-* 运行的评委是 qwen3.8-max（judge_qwen）。
    "judge": {"cli": "kimi", "model": "kimi-code/k3"},
    "judge2": {"cli": "kimi", "model": "kimi-code/k3"},
    "judge_qwen": {"base_url_env": "OPENAI_BASE_URL", "model": "qwen3.8-max", "key_env": "DASHSCOPE_API_KEY"},
    "judge_glm": {"base_url_env": "OPENAI_BASE_URL", "model": "glm-5.2", "key_env": "DASHSCOPE_API_KEY"},
    # 2026-09-17：Kimi 会员月额度与 token-plan 周额度同时耗尽，评委改走 WorkBuddy 内置的 CodeBuddy Code CLI
    # （用户的 WorkBuddy 登录态，按积分计费）。--help 里的模型列表只是静态子集：实测 --model 可直接用
    # glm-5.3 / kimi-k3 / kimi-k3-1 / deepseek-v4-flash / glm-5.2 等最新模型。缺省 kimi-k3：与 MemCompass v0.3
    # 的评委同一模型（当时经 Kimi CLI 调用）但单次约 3 积分；用户 2026-09-17 定：评委用 glm-5.3-flash（约 0.07 积分/次），
    # 答题器与被测系统内部 LLM 用 deepseek-v4-flash（约 0.04 积分/次），不再调 DeepSeek 官方 API。
    "judge_codebuddy": {"cli": "codebuddy", "model": "glm-5.3-flash"},
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
        self._new: dict[str, bytes] = {}  # 上次保存后新增的向量，增量落盘
        if path and path.exists():
            try:
                self.cache = pickle.loads(path.read_bytes())
            except Exception:  # noqa: BLE001
                self.cache = {}
        if path:
            for part in sorted(self._parts_dir().glob("*.pkl")):
                try:
                    self.cache.update(pickle.loads(part.read_bytes()))
                except Exception:  # noqa: BLE001
                    pass

    def _parts_dir(self) -> Path:
        return self.path.with_name(self.path.stem + ".parts")

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
                    b = array.array("f", v).tobytes()
                    self.cache[self._k(t)] = b
                    self._new[self._k(t)] = b
        out = []
        with self._lock:
            for k in keys:
                a = array.array("f")
                a.frombytes(self.cache[k])
                out.append(list(a))
        return out

    def save(self) -> None:
        """只把新增向量写成一个分片文件。整库重写要在内存里拼出整个 pickle（缓存到
        数百 MB 时会 MemoryError，并连带当前题失败）；缓存只是加速，保存失败不影响结果。"""
        import pickle
        import time

        if not (self.path and self._new):
            return
        with self._lock:
            new, self._new = self._new, {}
        try:
            d = self._parts_dir()
            d.mkdir(parents=True, exist_ok=True)
            name = f"{time.time_ns()}-{threading.get_ident()}"
            tmp = d / f"{name}.tmp"
            tmp.write_bytes(pickle.dumps(new))
            os.replace(tmp, d / f"{name}.pkl")
        except Exception as e:  # noqa: BLE001
            print(f"[embedding cache] save skipped: {type(e).__name__}", file=sys.stderr)


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
            value = m.group(2).strip()
            if not value.startswith(("'", '"')):
                value = re.sub(r"\s+#.*$", "", value)  # 行内注释（KEY=value  # 说明）不算值
            env[m.group(1)] = value.strip('"').strip("'")
    return env


def extract_usage(resp) -> dict | None:
    """从 OpenAI 兼容响应取 token 用量。completion_tokens 已含思考 token（DeepSeek / OpenAI 推理模型口径），
    reasoning_tokens 单列；整体拿不到 usage 返回 None。与 agent_memory.llm.OpenAILLMClient.extract_usage 同口径，
    这里独立一份，因为基线 worktree 里的 agent_memory 没有该方法。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None

    def _int(obj, name):
        v = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        return int(v) if isinstance(v, (int, float)) else 0

    details = getattr(usage, "completion_tokens_details", None)
    pdetails = getattr(usage, "prompt_tokens_details", None)
    return {
        "prompt_tokens": _int(usage, "prompt_tokens"),
        "completion_tokens": _int(usage, "completion_tokens"),
        "reasoning_tokens": _int(details, "reasoning_tokens") if details is not None else 0,
        "cached_tokens": _int(pdetails, "cached_tokens") if pdetails is not None else 0,
    }


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
        # 最近一次调用的 token 用量（API usage 字段；缓存命中时回放；拿不到为 None）
        self.last_usage: dict | None = None

    def _key(self, system: str, user: str, extra: str) -> str:
        import hashlib

        return hashlib.sha256(json.dumps([self.model, system, user, extra], ensure_ascii=False).encode()).hexdigest()

    def complete_json(self, system: str, user: str, schema: str, seed_tag: str = "") -> dict:
        key = self._key(system, user, schema + seed_tag)
        if self.cache_dir:
            f = self.cache_dir / f"{key}.json"
            if f.exists():
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                    self.last_usage = rec.get("usage")  # v0.3 以前的缓存没有这个字段 → None
                    return rec["response"]
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
                self.last_usage = extract_usage(resp)
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise ValueError("JSON 不是 object")
                if self.cache_dir:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
                    tmp.write_text(json.dumps({"model": self.model, "response": parsed, "usage": self.last_usage},
                                              ensure_ascii=False), encoding="utf-8")
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


class CostMeter:
    """按任务统计 LLM 用量（Q1 成本）：调用次数、输入 / 输出字符数，以及 API usage 字段给出的 token 数。

    字符数对每次调用都有；token 数只在客户端能拿到 usage 时累加（OpenAI 兼容 API 有，Kimi CLI 没有；
    v0.3 以前写入的缓存记录也没有），`token_calls` 记有 token 数的调用次数，报告据此给出覆盖率。
    out_tokens 按 API 口径已含思考 token，reasoning_tokens 单列——这是字符数低估成本的根源。
    """

    def __init__(self):
        self.calls = 0
        self.in_chars = 0
        self.out_chars = 0
        self.token_calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.credit = 0.0  # WorkBuddy / CodeBuddy CLI 的积分（API 客户端没有，保持 0）
        self._lock = threading.Lock()

    def add(self, in_chars: int, out_chars: int, usage: dict | None = None) -> None:
        with self._lock:
            self.calls += 1
            self.in_chars += in_chars
            self.out_chars += out_chars
            if usage:
                self.token_calls += 1
                self.in_tokens += int(usage.get("prompt_tokens") or 0)
                self.out_tokens += int(usage.get("completion_tokens") or 0)
                self.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
                self.cached_tokens += int(usage.get("cached_tokens") or 0)
                self.credit += float(usage.get("credit") or 0)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls, "in_chars": self.in_chars, "out_chars": self.out_chars,
            "token_calls": self.token_calls, "in_tokens": self.in_tokens, "out_tokens": self.out_tokens,
            "reasoning_tokens": self.reasoning_tokens, "cached_tokens": self.cached_tokens,
            "credit": round(self.credit, 4),
        }


class MeteredLLM:
    """包在 LLM 客户端外面计数。评测缓存命中也计入：统计的是系统本来要花的量，与缓存无关。
    同时兼容 agent_memory 的 LLMClient（complete / complete_json）与本模块的 ChatClient。"""

    def __init__(self, inner, meter: CostMeter):
        self.inner = inner
        self.meter = meter

    def _usage(self) -> dict | None:
        # 基线 worktree 的 LLMClient 与 Kimi CLI 客户端没有 last_usage → None，只计字符
        return getattr(self.inner, "last_usage", None)

    def complete(self, system: str, user: str) -> str:
        out = self.inner.complete(system, user)
        self.meter.add(len(system) + len(user), len(out or ""), self._usage())
        return out

    def complete_json(self, system: str, user: str, schema: str, *args, **kwargs) -> dict:
        out = self.inner.complete_json(system, user, schema, *args, **kwargs)
        self.meter.add(len(system) + len(user) + len(schema), len(json.dumps(out, ensure_ascii=False)), self._usage())
        return out

    def __getattr__(self, name):
        return getattr(self.inner, name)


class KimiCLIClient:
    """Kimi Code CLI 客户端（用户会员登录，不走 API）：`kimi -p` 非交互调用，取 stream-json 里最后一条助手回复。

    - 与 ChatClient 同接口（complete_json）、同一磁盘缓存，缓存键含模型名；
    - 子进程关掉用户的 agent-memory hook（环境变量）与技能（空的 --skills-dir），在临时空目录里运行，
      评测内容不会进入用户真实的记忆库；
    - CLI 不能设温度，复现靠缓存；并发由 MC_KIMI_CONCURRENCY 控制（默认 4），失败按退避重试。
    """

    def __init__(self, role: str, model: str = "kimi-code/k3", cache: bool = True, timeout: float = 420):
        self.role = role
        self.model = model
        self.base_url = "kimi-cli"
        self.cache_dir = CACHE_DIR / "memcompass" if cache else None
        self.exe = shutil.which("kimi") or str(Path.home() / ".kimi-code" / "bin" / "kimi.exe")
        self.workdir = Path(tempfile.gettempdir()) / "memcompass-kimi-cwd"
        self.skills = self.workdir / "empty-skills"
        self.skills.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._sem = threading.Semaphore(int(os.environ.get("MC_KIMI_CONCURRENCY", "4")))

    def _key(self, system: str, user: str, extra: str) -> str:
        import hashlib

        return hashlib.sha256(json.dumps([self.model, system, user, extra], ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _last_reply(stdout: str) -> str:
        text = ""
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and ev.get("role") == "assistant" and isinstance(ev.get("content"), str):
                text = ev["content"]
        return text

    @staticmethod
    def _parse(text: str) -> dict:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            i, j = text.find("{"), text.rfind("}")
            if i < 0 or j <= i:
                raise ValueError("回复里没有 JSON object") from None
            parsed = json.loads(text[i:j + 1])
        if not isinstance(parsed, dict):
            raise ValueError("JSON 不是 object")
        return parsed

    def complete_json(self, system: str, user: str, schema: str, seed_tag: str = "") -> dict:
        key = self._key(system, user, schema + seed_tag)
        if self.cache_dir:
            f = self.cache_dir / f"{key}.json"
            if f.exists():
                try:
                    return json.loads(f.read_text(encoding="utf-8"))["response"]
                except (OSError, json.JSONDecodeError, KeyError):
                    pass
        if len(user) > 24000:  # Windows 命令行上限约 32K 字符；只截中间，保留开头与结尾
            user = user[:12000] + "\n……（中间过长已省略）……\n" + user[-11000:]
        prompt = (f"{system}\n\n只输出一个 JSON object（不要 markdown 代码块，不要调用任何工具，不要任何解释），结构：\n"
                  f"{schema}\n\n===== 待评材料 =====\n{user}")
        env = os.environ | {"AGENT_MEMORY_WM_HOOK": "off", "AGENT_MEMORY_REVIEW_TURN_INTERVAL": "1000000"}
        cmd = [self.exe, "-p", prompt, "-m", self.model, "--skills-dir", str(self.skills), "--output-format", "stream-json"]
        backoff = [10, 30, 60, 120]
        last: Exception | None = None
        for attempt in range(len(backoff) + 1):
            try:
                with self._sem:
                    with _count_lock:
                        llm_calls[self.role] = llm_calls.get(self.role, 0) + 1
                    proc = subprocess.run(cmd, cwd=self.workdir, env=env, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", timeout=self.timeout)
                reply = self._last_reply(proc.stdout)
                if proc.returncode != 0 or not reply:
                    raise RuntimeError(f"kimi 退出码 {proc.returncode}：{(proc.stderr or proc.stdout)[-300:]}")
                parsed = self._parse(reply)
                if self.cache_dir:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
                    tmp.write_text(json.dumps({"model": self.model, "response": parsed}, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, self.cache_dir / f"{key}.json")
                return parsed
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < len(backoff):
                    time.sleep(backoff[attempt])
                    continue
        raise RuntimeError(f"Kimi CLI 调用失败：{last}")


def build_client(role: str, env: dict[str, str], override_model: str | None = None, cache: bool = True):
    spec = DEFAULTS[role]
    if spec.get("cli") == "kimi":
        return KimiCLIClient(role, override_model or spec["model"], cache=cache)
    if spec.get("cli") == "codebuddy":
        return CodeBuddyCLIClient(role, override_model or spec["model"], cache=cache)
    base = spec.get("base_url") or env.get(spec.get("base_url_env", ""), "")
    key = env.get(spec["key_env"]) or os.environ.get(spec["key_env"])
    if not base or not key:
        raise RuntimeError(f"{role} 的端点或密钥缺失（需要 --env-file 中的 {spec['key_env']}"
                           + (f" 与 {spec['base_url_env']}" if spec.get("base_url_env") else "") + "）")
    return ChatClient(role, base, key, override_model or spec["model"], cache=cache)


class CodeBuddyCLIClient:
    """CodeBuddy Code CLI 客户端（WorkBuddy 桌面应用内置，复用其登录态，按积分计费，不走 API key）。

    - `codebuddy -p --output-format json`：提示词经 stdin 传入（不受 Windows 命令行 32K 上限约束），
      取输出 JSON 数组里 type=result 的元素：`result` 为回复文本，`usage` / `credit` 为用量与积分；
    - 与 ChatClient 同接口（complete_json）并另有 complete（自由文本），可同时充当评委、答题器、
      以及 agent_memory 的 LLMClient（MeteredLLM 包一层即可）；同一磁盘缓存，缓存键含模型名；
    - 子进程：--tools "" 关掉全部工具、--strict-mcp-config 空配置（不会拉起用户的 agent-memory MCP）、
      --no-session-persistence、临时空目录作 cwd，评测内容不进用户的真实会话与记忆；
    - 不能设温度，复现靠缓存；并发由 MC_CODEBUDDY_CONCURRENCY 控制（默认 3）；
    - CLI 位置：环境变量 CODEBUDDY_CLI_DIR，否则 PATH 上的 codebuddy，否则 WorkBuddy 安装目录里的 bundle。
    """

    DEFAULT_CLI_DIRS = (
        Path.home() / "AppData" / "Local" / "Programs" / "WorkBuddy" / "resources" / "app.asar.unpacked" / "cli",
    )

    def __init__(self, role: str, model: str = "glm-5.1", cache: bool = True, timeout: float = 600):
        self.role = role
        self.model = model
        self.base_url = "codebuddy-cli"
        self.cache_dir = CACHE_DIR / "memcompass" if cache else None
        self.cmd_prefix = self._resolve_cli()
        self.workdir = Path(tempfile.gettempdir()) / "memcompass-codebuddy-cwd"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.last_usage: dict | None = None
        self._sem = threading.Semaphore(int(os.environ.get("MC_CODEBUDDY_CONCURRENCY", "2")))
        self.fail_dir = CACHE_DIR.parent / "aml_selftest" / "codebuddy_failures"  # 失败时的 stdout/stderr 片段，便于事后排查
        # CLI 每次启动都在 TEMP 下解一份插件市场包（codebuddy-marketplace-install-*，5–17 MB）且不清理，
        # 2026-09-23 攒到 6000+ 个把 D 盘写满。每次调用给独立 TEMP，调用完整目录删掉；启动时清上次崩溃的残余。
        self.tmp_root = Path(tempfile.gettempdir()) / "memcompass-codebuddy-tmp"
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        for old in self.tmp_root.iterdir():
            try:
                if time.time() - old.stat().st_mtime > 3600:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass

    @classmethod
    def _resolve_cli(cls) -> list[str]:
        d = os.environ.get("CODEBUDDY_CLI_DIR")
        dirs = [Path(d)] if d else []
        dirs += list(cls.DEFAULT_CLI_DIRS)
        for cand in dirs:
            if (cand / "bin" / "codebuddy").exists():
                node = shutil.which("node")
                if not node:
                    raise RuntimeError("找不到 node，CodeBuddy CLI 需要 Node.js >= 18.20.8")
                return [node, str(cand / "bin" / "codebuddy")]
        exe = shutil.which("codebuddy") or shutil.which("cbc")
        if exe:
            return [exe]
        raise RuntimeError("找不到 CodeBuddy CLI：设 CODEBUDDY_CLI_DIR 指向 WorkBuddy 的 resources/app.asar.unpacked/cli")

    def _key(self, kind: str, system: str, user: str, extra: str) -> str:
        import hashlib

        return hashlib.sha256(json.dumps([self.model, kind, system, user, extra], ensure_ascii=False).encode()).hexdigest()

    def _cache_get(self, key: str):
        if self.cache_dir and (self.cache_dir / f"{key}.json").exists():
            try:
                rec = json.loads((self.cache_dir / f"{key}.json").read_text(encoding="utf-8"))
                self.last_usage = rec.get("usage")
                return rec["response"]
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        return None

    def _cache_put(self, key: str, response) -> None:
        if not self.cache_dir:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps({"model": self.model, "response": response, "usage": self.last_usage},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cache_dir / f"{key}.json")

    def _run(self, system: str, prompt: str) -> str:
        cmd = self.cmd_prefix + [
            "-p", "--model", self.model, "--output-format", "json", "--tools", "", "--max-turns", "1",
            "--no-session-persistence", "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
            "--system-prompt", system or "You are a careful assistant. Output only what is asked, nothing else.",
        ]
        backoff = [5, 10, 20, 40]  # 2026-09-17 晚：长退避把吞吐拖到每会话 4 分钟，改短；失败原样落盘排查
        last: Exception | None = None
        for attempt in range(len(backoff) + 1):
            try:
                with self._sem:
                    with _count_lock:
                        llm_calls[self.role] = llm_calls.get(self.role, 0) + 1
                    call_tmp = tempfile.mkdtemp(prefix="cb-", dir=self.tmp_root)
                    env = {**os.environ, "TEMP": call_tmp, "TMP": call_tmp, "TMPDIR": call_tmp}
                    try:
                        proc = subprocess.run(cmd, cwd=self.workdir, input=prompt, capture_output=True, text=True,
                                              encoding="utf-8", errors="replace", timeout=self.timeout, env=env)
                    finally:
                        shutil.rmtree(call_tmp, ignore_errors=True)
                if proc.returncode != 0:
                    self._dump_failure(proc, f"exit {proc.returncode}")
                    raise RuntimeError(f"codebuddy 退出码 {proc.returncode}：{(proc.stderr or proc.stdout)[-300:]}")
                out = proc.stdout or ""
                start = out.find("[")
                if start < 0:
                    self._dump_failure(proc, "no json array")
                    raise RuntimeError(f"codebuddy 输出不是 JSON（timeout/临时故障）：{(proc.stderr or out)[:300]!r}")
                try:
                    events = json.loads(out[start:])
                except json.JSONDecodeError as e:
                    self._dump_failure(proc, f"json error {e}")
                    raise RuntimeError(f"codebuddy 输出 JSON 解析失败（timeout/临时故障）：{out[start:start + 200]!r}") from e
                result = [e for e in events if isinstance(e, dict) and e.get("type") == "result"]
                if not result:
                    raise RuntimeError("codebuddy 输出里没有 result 事件")
                res = result[-1]
                if res.get("is_error"):
                    raise RuntimeError(f"codebuddy 报错：{str(res.get('result'))[:300]}")
                self.last_usage = self._usage_from(events, res)
                return str(res.get("result") or "")
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).lower()
                # CLI 子进程崩溃（如 0xC0000409 fail-fast）与网络类错误一样按临时故障重试；确定性错误（模型 id 无效等）不重试
                transient = any(t in msg for t in ("timeout", "timed out", "429", "rate", "overload", "busy",
                                                    "connection", "502", "503", "econnreset", "没有 result",
                                                    "退出码 3221", "退出码 -", "退出码 1：", "退出码 134", "退出码 139"))
                if "valid model" in msg or "invalid" in msg and "model" in msg:
                    transient = False
                if attempt < len(backoff) and transient:
                    time.sleep(backoff[attempt])
                    continue
                raise
        raise RuntimeError(f"codebuddy 调用失败：{last}")

    def _dump_failure(self, proc, why: str) -> None:
        try:
            self.fail_dir.mkdir(parents=True, exist_ok=True)
            f = self.fail_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{threading.get_ident()}.txt"
            body = chr(10).join([why, f"model={self.model}", "--- stderr ---", (proc.stderr or "")[-3000:],
                                   "--- stdout head ---", (proc.stdout or "")[:3000], ""])
            f.write_text(body, encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _usage_from(events: list, res: dict) -> dict:
        u = res.get("usage") or {}
        out = {"prompt_tokens": int(u.get("input_tokens") or 0), "completion_tokens": int(u.get("output_tokens") or 0),
               "reasoning_tokens": 0, "cached_tokens": int(u.get("cache_read_input_tokens") or 0), "credit": 0.0}
        # 每条 message 事件的 providerData.rawUsage 里带 completion_thinking_tokens 与 credit（积分），累加
        for e in events:
            ru = ((e.get("providerData") or {}).get("rawUsage") or {}) if isinstance(e, dict) else {}
            out["reasoning_tokens"] += int(ru.get("completion_thinking_tokens") or 0)
            out["credit"] += float(ru.get("credit") or 0)
        out["credit"] = round(out["credit"], 4)
        return out

    def complete(self, system: str, user: str) -> str:
        key = self._key("text", system, user, "")
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        text = self._run(system, user).strip()
        if not text:
            raise RuntimeError("codebuddy 返回了空内容")
        self._cache_put(key, text)
        return text

    def complete_json(self, system: str, user: str, schema: str, seed_tag: str = "") -> dict:
        key = self._key("json", system, user, schema + seed_tag)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        sys_full = (f"{system}\n\n只输出一个 JSON object（不要 markdown 代码块，不要调用任何工具，不要任何解释），"
                    f"结构：\n{schema}")
        text = self._run(sys_full, user)
        parsed = KimiCLIClient._parse(text)
        try:  # 与 agent_memory 的 OpenAILLMClient 同样做一次 schema 层校验（可用时）
            from agent_memory.llm import validate_json_response

            parsed = validate_json_response(parsed, schema)
        except Exception:  # noqa: BLE001  runner 独立于 agent_memory 版本时跳过
            pass
        self._cache_put(key, parsed)
        return parsed


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
