"""scripts/memory_turn_hook.py 测试：Stop hook 的轮次计数与强制更新触发。

hook 是独立脚本，用子进程 + stdin JSON 的方式端到端验证退出码语义：
退出码 0 = 放行（未计满）；退出码 2 = 拦截本轮结束并注入蒸馏指令（计满）。
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "scripts" / "memory_turn_hook.py"


def _run_hook(data_dir: Path, session_id: str = "s1", interval: str = "3"):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "Stop", "session_id": session_id}),
        capture_output=True,
        text=True,
        encoding="utf-8",  # hook 输出统一为 UTF-8（宿主按 UTF-8 读取）
        env={
            "AGENT_MEMORY_DATA_DIR": str(data_dir),
            "AGENT_MEMORY_REVIEW_TURN_INTERVAL": interval,
            "PATH": "",
        },
    )


def _count(data_dir: Path, session_id: str = "s1") -> int:
    counter_file = data_dir / "state" / "turn_counter.json"
    if not counter_file.exists():
        return 0
    return json.loads(counter_file.read_text(encoding="utf-8")).get(session_id, 0)


def test_pass_through_before_interval(tmp_path):
    assert _run_hook(tmp_path).returncode == 0
    assert _count(tmp_path) == 1
    assert _run_hook(tmp_path).returncode == 0
    assert _count(tmp_path) == 2


def test_third_turn_triggers_instruction_and_resets(tmp_path):
    _run_hook(tmp_path)
    _run_hook(tmp_path)
    third = _run_hook(tmp_path)
    assert third.returncode == 2
    assert "强制记忆更新" in third.stderr
    assert "memory_add" in third.stderr
    assert "确认" in third.stderr  # 指令含用户确认资格规则
    assert _count(tmp_path) == 0  # 触发后清零重新计
    # 下一轮重新开始计数
    assert _run_hook(tmp_path).returncode == 0
    assert _count(tmp_path) == 1


def test_sessions_count_independently(tmp_path):
    _run_hook(tmp_path, session_id="a")
    _run_hook(tmp_path, session_id="a")
    # 另一个 session 的轮次不影响本 session
    assert _run_hook(tmp_path, session_id="b").returncode == 0
    assert _run_hook(tmp_path, session_id="a").returncode == 2


def test_interval_env_override(tmp_path):
    assert _run_hook(tmp_path, interval="1").returncode == 2


def test_invalid_stdin_fails_open(tmp_path):
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={"AGENT_MEMORY_DATA_DIR": str(tmp_path), "PATH": ""},
    )
    assert result.returncode == 0


def test_corrupt_counter_fails_open(tmp_path):
    counter_file = tmp_path / "state" / "turn_counter.json"
    counter_file.parent.mkdir(parents=True)
    counter_file.write_text("{broken", encoding="utf-8")
    assert _run_hook(tmp_path).returncode == 0
    assert _count(tmp_path) == 1
