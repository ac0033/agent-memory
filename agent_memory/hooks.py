"""宿主 hook 的实现（`agent-memory hook <名字>` 调用这里）。

三个 hook，宿主中立——宿主差异只在注册到哪个事件上：

- wm-inject：会话开头注入工作记忆。直接读 data/working 文件，不依赖后台进程。
  注册到"会话开始"事件（如 SessionStart）时每次触发都注入（压缩、恢复后也会补上）；
  只有"用户提交消息"事件可用的宿主加 --once，每个会话只注入一次。
- surface：用户每提交一条消息，请后台进程判断要不要主动提醒。后台进程没在跑时
  只负责把它拉起、本次静默放行（加载模型要十几秒，hook 等不起）。
  AGENT_MEMORY_SURFACE_HOOK=off 关闭。
- turn：每 N 轮（review_turn_interval）以退出码 2 拦截一次本轮结束，注入蒸馏指令。
  纯本地计数，不依赖后台进程。

全部按宿主惯例 fail-open：配置错误、服务不可达、状态文件损坏都静默放行（退出码 0），
不影响对话。配置口径与服务端一致（配置文件叠加环境变量，见 config.py）。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from agent_memory.config import Settings, get_settings
from agent_memory.io_utils import atomic_write_text, interprocess_lock

_MAX_TRACKED_SESSIONS = 500
_SURFACE_RECENT = 3
_SURFACE_MAX_SESSIONS = 200


@dataclass
class HookResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


def _slugify(name: str) -> str:
    """目录名 -> scope slug：小写、非法字符段折叠为单个连字符（与服务端归一化同口径）。"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _debug(settings: Settings, env: dict[str, str], name: str, payload: dict) -> None:
    if env.get("AGENT_MEMORY_HOOK_DEBUG") != "1":
        return
    try:
        path = settings.data_dir / "logs" / f"hook_debug_{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(UTC).isoformat(), "payload": payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}  # 状态损坏按空处理（fail-open）


# ---------------------------------------------------------------- wm-inject


def derive_scopes(cwd: str, agent_name: str | None) -> list[str]:
    """global（总是）+ repo:<当前目录名>（能推出合法 slug 时）+ agent:<宿主名>（给了才加）。"""
    scopes = ["global"]
    slug = _slugify(Path(cwd).name)
    if slug:
        scopes.append(f"repo:{slug}")
    agent_slug = _slugify(agent_name or "")
    if agent_slug:
        scopes.append(f"agent:{agent_slug}")
    return scopes


def render_wm_blocks(settings: Settings, scopes: list[str]) -> str:
    """各 scope 的非空工作记忆渲染块，格式与 HTTP /wm_blocks 一致。"""
    from agent_memory.models import is_valid_scope, normalize_scope
    from agent_memory.working.render import render_working_memory_block
    from agent_memory.working.store import WorkingMemoryStore

    store = WorkingMemoryStore(settings.data_dir)
    parts: list[str] = []
    for scope in scopes:
        normalized = normalize_scope(scope)
        if not is_valid_scope(normalized):
            continue
        wm = store.read(normalized)
        if wm is None:
            continue
        block = render_working_memory_block(wm, settings.working_memory_budget_chars)
        if block:
            parts.append(f"### scope: {normalized}\n\n{block}")
    return "\n\n".join(parts)


def wm_inject(
    payload: dict, env: dict[str, str], *, agent: str | None = None, once: bool = False
) -> HookResult:
    if env.get("AGENT_MEMORY_WM_HOOK", "").lower() == "off":
        return HookResult()
    settings = get_settings(env)
    _debug(settings, env, "wm", payload)
    cwd = str(payload.get("cwd") or env.get("PWD") or Path.cwd())
    scopes = derive_scopes(cwd, agent or env.get("AGENT_MEMORY_AGENT_NAME"))
    if not once:
        return HookResult(stdout=render_wm_blocks(settings, scopes))
    # --once：每个会话只注入一次。锁覆盖"检查→渲染→记账"，避免并发的两次触发都注入
    session_id = str(payload.get("session_id") or "default")
    state_dir = settings.data_dir / "state"
    state_file = state_dir / "wm_injected_sessions.json"
    with interprocess_lock(state_dir / "wm_hook.lock"):
        injected = _load_json_dict(state_file)
        if session_id in injected:
            return HookResult()
        text = render_wm_blocks(settings, scopes)
        injected[session_id] = True
        while len(injected) > _MAX_TRACKED_SESSIONS:
            injected.pop(next(iter(injected)))
        atomic_write_text(state_file, json.dumps(injected, ensure_ascii=False))
    return HookResult(stdout=text)


# ---------------------------------------------------------------- surface


def _remember_recent(settings: Settings, session_id: str, message: str) -> list[str]:
    """记下本会话最近几条消息作线索，返回本条之前的几条。"""
    state_dir = settings.data_dir / "state"
    state_file = state_dir / "surface_recent_turns.json"
    with interprocess_lock(state_dir / "surface_hook.lock"):
        state = _load_json_dict(state_file)
        recent = [str(x) for x in state.get(session_id) or []][-_SURFACE_RECENT:]
        state.pop(session_id, None)
        state[session_id] = (recent + [message[:500]])[-_SURFACE_RECENT:]
        while len(state) > _SURFACE_MAX_SESSIONS:
            state.pop(next(iter(state)))
        atomic_write_text(state_file, json.dumps(state, ensure_ascii=False))
    return recent


def _start_daemon_in_background(settings: Settings) -> None:
    from agent_memory.server import daemon

    def target() -> None:
        try:
            daemon.ensure_running(settings, wait=False)
        except Exception:
            pass

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=3.0)  # 只给拉起动作几秒；拿不到启动锁就算了，下一条消息再试


def surface(payload: dict, env: dict[str, str]) -> HookResult:
    if env.get("AGENT_MEMORY_SURFACE_HOOK", "").lower() == "off":
        return HookResult()
    raw = payload.get("prompt") or payload.get("user_prompt") or payload.get("message") or ""
    message = str(raw).strip()
    if not message:
        return HookResult()
    settings = get_settings(env)
    _debug(settings, env, "surface", payload)
    from agent_memory.server import daemon

    try:
        info = daemon.probe(settings)
    except daemon.DaemonError:
        return HookResult()  # 端口被别的进程占着：不拉起、不打扰
    try:
        recent = _remember_recent(settings, str(payload.get("session_id") or "default"), message)
    except Exception:
        recent = []
    if info is None:
        _start_daemon_in_background(settings)
        return HookResult()
    slug = _slugify(Path(str(payload.get("cwd") or Path.cwd())).name)
    body = json.dumps(
        {
            "message": message,
            "scope": f"repo:{slug}" if slug else "global",
            "recent_turns": recent,
            "date": date.today().isoformat(),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        daemon.base_url(settings) + "/surface",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = float(env.get("AGENT_MEMORY_SURFACE_TIMEOUT", "25"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    return HookResult(stdout=text)


# ---------------------------------------------------------------- turn

TURN_INSTRUCTION = """[agent-memory 强制记忆更新]
已到每 {interval} 轮一次的强制记忆更新点。请立刻执行：
1. 把最近 {interval} 轮对话（含刚结束的这轮）整理成 conversation JSON——
   每轮材料 = 用户的原始消息 + 紧邻其前的你的回复；
2. 顺手用 memory_wm_write 同步工作记忆——逐项检查目标/待办/决策/变量/
   备注是否仍然准确（刚完成的待办标 done、被推翻的决策删除、变量更新为
   当前值），不是只追加新状态；它是全量替换而非合并，写时带上检查后的
   完整状态，并把 turn_watermark 更新为当前轮数；
3. 调 memory_add（conversation_json 模式）走蒸馏管线——conversation_json
   推荐传 JSON 字符串（把数组序列化后再传；直接传数组服务端也会兼容）。
   只沉淀用户明确确认
   或同意过的内容：用户自己的陈述/要求/偏好可直接沉淀；你单方面提出而
   用户未表态的建议、方案、结论一律不沉淀。若蒸馏返回 archived_only 且
   原因是未配置服务端 LLM：改走宿主蒸馏——调 memory_distill_prompt 拿
   蒸馏协议，自行蒸馏后以 memory_add(distilled_json=...) 提交；
4. 若返回的 pending_review 非空，逐条向用户报告（内容 + 排队原因）并请其
   裁决：approve 入库 / modify 修改后入库 / discard 丢弃，用
   memory_review_resolve 落地；
5. 若本会话没有 memory_add 可用，或这段对话确实没有值得沉淀的内容，
   向用户说明一句即可。完成后正常结束本轮。"""


def turn(payload: dict, env: dict[str, str]) -> HookResult:
    if payload.get("stop_hook_active"):
        return HookResult()  # 本轮就是被本 hook 拦下后继续的：不重复计数，避免连环拦截
    settings = get_settings(env)
    _debug(settings, env, "turn", payload)
    interval = settings.review_turn_interval
    state_dir = settings.data_dir / "state"
    counter_file = state_dir / "turn_counter.json"
    session_id = str(payload.get("session_id") or "default")
    with interprocess_lock(state_dir / "turn_hook.lock"):
        counters = _load_json_dict(counter_file)
        count = int(counters.get(session_id, 0)) + 1
        counters[session_id] = 0 if count >= interval else count
        atomic_write_text(counter_file, json.dumps(counters, ensure_ascii=False))
    if count < interval:
        return HookResult()
    return HookResult(exit_code=2, stderr=TURN_INSTRUCTION.format(interval=interval))
