"""按需启动的本机后台进程：生命周期管理（启动 / 探活 / 停止 / 版本对齐）。

后台进程就是 http_server（只绑回环地址），它的存在理由是让嵌入模型在本机只加载
一份、供所有会话与 hook 共用。它不是开机常驻服务：

- 由 stdio 转发层（`agent-memory mcp`）或 hook 在需要时拉起，普通用户权限即可；
- 空闲 `daemon_idle_minutes` 分钟后自己退出，释放内存；
- 代码指纹（包内 .py 文件的路径、大小、修改时间 + 版本号）与磁盘上的代码不一致时，
  转发层会把它停掉重启，改了代码不需要手动重启。

同一时刻只有一个后台进程：启动过程在 `<data_dir>/state/daemon.lock` 下串行化，
端口本身也只能被一个进程占用。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_memory import __version__
from agent_memory.config import Settings
from agent_memory.io_utils import interprocess_lock

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_PROBE_TIMEOUT_SECONDS = 1.5
DEFAULT_START_TIMEOUT_SECONDS = 180.0


class DaemonError(RuntimeError):
    """后台进程无法就绪（确定性原因写在消息里：端口被占用、进程启动即退出等）。"""


def code_fingerprint() -> str:
    """包内全部 .py 文件的相对路径、大小、修改时间，加上版本号，取 sha256 前 16 位。"""
    digest = hashlib.sha256(__version__.encode())
    for path in sorted(_PACKAGE_DIR.rglob("*.py")):
        stat = path.stat()
        rel = path.relative_to(_PACKAGE_DIR).as_posix()
        digest.update(f"{rel}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def base_url(settings: Settings) -> str:
    return f"http://{settings.http_host}:{settings.http_port}"


def mcp_url(settings: Settings) -> str:
    return base_url(settings) + "/mcp"


def log_path(settings: Settings) -> Path:
    return settings.data_dir / "logs" / "daemon.log"


def probe(settings: Settings) -> dict | None:
    """探活。返回 /health 的 JSON；端口没人监听返回 None。

    端口有人监听但不是本服务（没有 /health 或返回的不是 agent-memory）时抛
    DaemonError——那是别的进程占着端口，原样重试无效。
    """
    url = base_url(settings) + "/health"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise DaemonError(
            f"端口 {settings.http_port} 上的进程不是本版本的 agent-memory 后台进程"
            f"（/health 返回 {e.code}）。多半是旧的常驻服务还在运行：先结束它，"
            "或用 AGENT_MEMORY_HTTP_PORT 换一个端口。"
        ) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None
    try:
        info = json.loads(body)
    except json.JSONDecodeError as e:
        raise DaemonError(f"端口 {settings.http_port} 上的进程返回了非 JSON 的 /health") from e
    if not isinstance(info, dict) or info.get("service") != "agent-memory":
        raise DaemonError(f"端口 {settings.http_port} 被其他服务占用")
    return info


# 传给后台进程的环境变量：只挑记忆服务、模型缓存与网络代理相关的，其余不落盘
_ENV_PREFIXES = ("AGENT_MEMORY_", "HF_", "TRANSFORMERS_", "SENTENCE_TRANSFORMERS_", "TORCH_")
_ENV_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE",
              "REQUESTS_CA_BUNDLE"}


def _env_snapshot() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k.upper().startswith(_ENV_PREFIXES) or k.upper() in _ENV_NAMES
    }


def _daemon_command(settings: Settings, env_file: Path | None) -> list[str]:
    python = Path(sys.executable)
    if os.name == "nt" and (python.parent / "pythonw.exe").exists():
        python = python.parent / "pythonw.exe"  # 无控制台窗口
    cmd = [str(python), "-m", "agent_memory.server.http_server", "--log", str(log_path(settings))]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    return cmd


def _spawn(settings: Settings) -> int:
    """拉起后台进程，返回 PID。进程自己把输出写进 daemon.log。

    Windows 上经 WMI（Win32_Process.Create）创建：宿主结束会话时会连同子进程树、
    作业对象一起结束，经 WMI 创建的进程不属于宿主的进程树，能活过单个会话。
    WMI 创建的进程拿不到调用方的环境变量，所以把相关的几项写进一次性文件，
    后台进程读完即删。
    """
    log_path(settings).parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        proc = subprocess.Popen(
            _daemon_command(settings, None),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(settings.data_dir),
            start_new_session=True,
        )
        return proc.pid
    state_dir = settings.data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env_file = state_dir / "daemon_env.json"
    env_file.write_text(json.dumps(_env_snapshot(), ensure_ascii=False), encoding="utf-8")
    script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        "@{CommandLine=$env:AM_DAEMON_CMD; CurrentDirectory=$env:AM_DAEMON_CWD}; "
        "Write-Output \"$($r.ReturnValue) $($r.ProcessId)\""
    )
    env = dict(os.environ)
    env["AM_DAEMON_CMD"] = subprocess.list2cmdline(_daemon_command(settings, env_file))
    env["AM_DAEMON_CWD"] = str(settings.data_dir)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) != 2 or parts[0] != "0":
        env_file.unlink(missing_ok=True)
        raise DaemonError(
            f"无法创建后台进程（WMI Win32_Process.Create）：{result.stdout.strip()} "
            f"{result.stderr.strip()[:300]}"
        )
    return int(parts[1])


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # 已退出但未回收的子进程也算退出
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            return done == 0
        except ChildProcessError:
            return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _log_tail(settings: Settings, lines: int = 15) -> str:
    try:
        text = log_path(settings).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.kill(pid, 15)
    except OSError:
        pass


def _wait_until(predicate, timeout: float, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def stop(settings: Settings, timeout: float = 15.0) -> bool:
    """停掉后台进程。返回是否真的停了一个进程。"""
    info = probe(settings)
    if info is None:
        return False
    _kill(int(info["pid"]))
    if not _wait_until(lambda: _safe_probe(settings) is None, timeout):
        raise DaemonError(f"后台进程（PID {info['pid']}）{timeout:.0f} 秒内没有退出")
    return True


def _safe_probe(settings: Settings) -> dict | None:
    try:
        return probe(settings)
    except DaemonError:
        return {"foreign": True}


def ensure_running(
    settings: Settings, *, wait: bool = True, timeout: float = DEFAULT_START_TIMEOUT_SECONDS
) -> dict | None:
    """确保后台进程在跑且代码是最新的。返回 /health 信息；wait=False 时可能返回 None。

    - 在跑且指纹一致：直接返回；
    - 在跑但指纹不一致（代码改过）：停掉重启；
    - 没在跑：拉起。wait=False 只负责拉起、不等就绪（给超时很短的 hook 用）。
    """
    fingerprint = code_fingerprint()
    deadline = time.monotonic() + timeout
    while True:
        # 别的调用方正在拉起时，Windows 的文件锁约 10 秒拿不到就报错；
        # 这时看一眼它是不是已经就绪，没就绪就接着等，直到总超时
        try:
            return _ensure_locked(settings, fingerprint, wait=wait, timeout=timeout)
        except OSError as e:
            info = _safe_probe(settings)
            if info is not None and info.get("fingerprint") == fingerprint:
                return info
            if not wait or time.monotonic() >= deadline:
                raise DaemonError(f"等待其他调用方拉起后台进程超时：{e}") from e


def _ensure_locked(settings: Settings, fingerprint: str, *, wait: bool, timeout: float):
    with interprocess_lock(settings.data_dir / "state" / "daemon.lock"):
        info = probe(settings)
        if info is not None and info.get("fingerprint") == fingerprint:
            return info
        if info is not None:
            stop(settings)
        pid = _spawn(settings)
        if not wait:
            return None

        def ready() -> bool:
            if not _pid_alive(pid):
                return True
            current = _safe_probe(settings)
            return current is not None and current.get("fingerprint") == fingerprint

        _wait_until(ready, timeout, interval=0.5)
        if not _pid_alive(pid) and _safe_probe(settings) is None:
            raise DaemonError(f"后台进程启动后立即退出。日志末尾：\n{_log_tail(settings)}")
        info = probe(settings)
        if info is None or info.get("fingerprint") != fingerprint:
            raise DaemonError(
                f"后台进程 {timeout:.0f} 秒内没有就绪（可能仍在加载），稍后重试。"
                f"日志：{log_path(settings)}"
            )
        return info


def status(settings: Settings) -> dict:
    """结构化状态：running / stale（代码已改、待重启）/ stopped / foreign（端口被占）。"""
    try:
        info = probe(settings)
    except DaemonError as e:
        return {"state": "foreign", "detail": str(e), "url": base_url(settings)}
    if info is None:
        return {"state": "stopped", "url": base_url(settings)}
    state = "running" if info.get("fingerprint") == code_fingerprint() else "stale"
    return {"state": state, "url": base_url(settings), **info}
