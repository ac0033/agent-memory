"""公开卫生测试：仓库里只放产品本身。

守住三件事：跟踪的文本文件里没有本机路径、邮箱、密钥形状的字符串；私人材料目录与运行态目录在
.gitignore 里且没有被跟踪；版本号在 pyproject、包内 __version__ 与 CHANGELOG 顶部条目三处一致。
只有本机才知道的词放在 .notes/hygiene_private_words.txt（一行一个，不入库），存在时一并检查。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

import agent_memory

ROOT = Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt", ".sh", ".ps1", ".cmd", ".cfg", ".ini",
}
SKIP_FILES = {"uv.lock"}

# 通用示例路径（文档与测试夹具里故意写的占位）
ALLOWED_PATHS = (
    "C:/Users/<you>",
    "D:/work/",
    "D:/proj",
    "D:\\proj",
    "D:\\\\proj",  # 源码里写成 "D:\\\\proj" 的夹具
    "D:/x",
)
# 测试夹具与用例里的假密钥
ALLOWED_KEYS = {
    "sk-abcdefghijklmnopqrstuv",
    "sk-projAbCdEfGh1234567890",
    "sk-test1234567890abcdef",
    "sk-queue-celery-final",
}
# 匿名提交邮箱、评测数据里的保留域名、第三方公开联系邮箱
ALLOWED_EMAIL_DOMAINS = (
    "users.noreply.github.com",
    ".example.invalid",
    "example.com",
    "agentmemoryleaderboard.ai",
)
# 脱敏测试里的占位邮箱
ALLOWED_EMAILS = {"a@b.com", "c@d.com"}

WIN_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'`)\],;：]*")
UNIX_HOME = re.compile(r"(?:/Users|/home)/[A-Za-z0-9_.-]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


def _tracked_text_files() -> list[Path]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("不在 git 仓库里，跳过公开卫生检查")
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    files = []
    for rel in out.split("\0"):
        if not rel or rel in SKIP_FILES:
            continue
        p = ROOT / rel
        if p.suffix.lower() in TEXT_SUFFIXES and p.is_file():
            files.append(p)
    assert files, "git ls-files 没有返回任何文本文件"
    return files


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def _private_words() -> list[str]:
    f = ROOT / ".notes" / "hygiene_private_words.txt"
    if not f.exists():
        return []
    return [w.strip() for w in _read(f).splitlines() if w.strip() and not w.startswith("#")]


def test_no_local_paths_in_tracked_files():
    offenders = []
    for p in _tracked_text_files():
        text = _read(p)
        for m in WIN_PATH.findall(text):
            if not m.startswith(ALLOWED_PATHS):
                offenders.append(f"{p.relative_to(ROOT)}: {m}")
        for m in UNIX_HOME.findall(text):
            if m not in ("/Users/<you>", "/home/<you>"):
                offenders.append(f"{p.relative_to(ROOT)}: {m}")
    assert not offenders, "跟踪文件里出现本机路径：\n" + "\n".join(offenders)


def test_no_emails_or_secret_shaped_strings():
    offenders = []
    for p in _tracked_text_files():
        text = _read(p)
        for m in EMAIL.findall(text):
            if m not in ALLOWED_EMAILS and not m.endswith(ALLOWED_EMAIL_DOMAINS):
                offenders.append(f"{p.relative_to(ROOT)}: {m}")
        for m in SECRET.findall(text):
            if m not in ALLOWED_KEYS:
                offenders.append(f"{p.relative_to(ROOT)}: {m}")
    assert not offenders, "跟踪文件里出现邮箱或密钥形状的字符串：\n" + "\n".join(offenders)


def test_no_private_words():
    words = _private_words()
    if not words:
        pytest.skip(".notes/hygiene_private_words.txt 不存在")
    offenders = []
    for p in _tracked_text_files():
        text = _read(p)
        for w in words:
            if w in text:
                offenders.append(f"{p.relative_to(ROOT)}: {w}")
    assert not offenders, "跟踪文件里出现私人词：\n" + "\n".join(offenders)


def test_private_dirs_ignored_and_untracked():
    gitignore = _read(ROOT / ".gitignore").splitlines()
    for needed in ("/.notes/", "/.claude/", ".venv/", "__pycache__/", "dist/", ".env"):
        assert needed in gitignore, f".gitignore 缺少 {needed}"
    tracked = subprocess.run(
        ["git", "ls-files", ".notes", ".claude", "data", ".env"],
        cwd=ROOT, capture_output=True, check=True, text=True,
    ).stdout.splitlines()
    leaked = [t for t in tracked if not t.endswith(".gitkeep")]
    assert not leaked, f"私人或运行态文件被跟踪：{leaked}"


def test_version_consistent_across_pyproject_package_and_changelog():
    with (ROOT / "pyproject.toml").open("rb") as f:
        pyproject_version = tomllib.load(f)["project"]["version"]
    changelog = _read(ROOT / "docs" / "CHANGELOG.md")
    m = re.search(r"^## v(\d+\.\d+\.\d+)\b", changelog, re.M)
    assert m, "docs/CHANGELOG.md 顶部没有 '## vX.Y.Z' 条目"
    assert pyproject_version == agent_memory.__version__ == m.group(1), (
        f"版本号不一致：pyproject={pyproject_version}, "
        f"__version__={agent_memory.__version__}, CHANGELOG={m.group(1)}"
    )
