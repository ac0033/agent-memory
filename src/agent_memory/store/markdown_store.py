"""Markdown 记忆层存储（data/memory，唯一事实来源）。

目录约定：<data_dir>/memory/<scope 目录名>/<id>.md
- frontmatter 存 MemoryEntry 的全部元数据字段，正文就是 content；
- scope 里的冒号在 Windows 目录名中非法，写入时统一把 ":" 替换成 "__"，
  读取时还原（slug 只允许小写字母/数字/连字符，不会出现 "__"，映射无歧义）。

fail-closed：id 冲突、文件缺失、frontmatter 非法都直接抛异常，不做静默降级。
"""

import re
from datetime import date
from pathlib import Path

import yaml

from agent_memory.models import MemoryEntry

# scope -> 目录名的分隔符："repo:abc" -> "repo__abc"
SCOPE_DIR_SEP = "__"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class MemoryStoreError(ValueError):
    """记忆层读写错误（id 冲突、frontmatter 非法等），fail-closed 直接抛出。"""


class MemoryNotFoundError(KeyError):
    """按 id 找不到记忆条目时抛出。"""


def scope_to_dirname(scope: str) -> str:
    """把 scope 映射为合法目录名：冒号替换为 "__"。"""
    return scope.replace(":", SCOPE_DIR_SEP)


def dirname_to_scope(dirname: str) -> str:
    """目录名还原为 scope："__" 还原为冒号。"""
    return dirname.replace(SCOPE_DIR_SEP, ":")


def entry_to_markdown(entry: MemoryEntry) -> str:
    """MemoryEntry -> 带 YAML frontmatter 的 Markdown 文本。"""
    meta = entry.model_dump(exclude={"content"}, mode="json")
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{frontmatter}---\n\n{entry.content}\n"


def entry_from_markdown(text: str, *, path: Path | None = None) -> MemoryEntry:
    """Markdown 文本 -> MemoryEntry。frontmatter 缺失或非法直接报错。"""
    where = f"（{path}）" if path else ""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise MemoryStoreError(f"记忆文件缺少合法的 YAML frontmatter{where}")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise MemoryStoreError(f"frontmatter YAML 解析失败{where}: {e}") from e
    if not isinstance(meta, dict):
        raise MemoryStoreError(f"frontmatter 必须是 YAML 映射{where}")
    meta["content"] = match.group(2).strip()
    try:
        return MemoryEntry.model_validate(meta)
    except ValueError as e:
        raise MemoryStoreError(f"frontmatter 字段校验失败{where}: {e}") from e


class MarkdownStore:
    """data/memory 层的 CRUD。id 在整个 store 内全局唯一（跨 scope）。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.memory_dir = self.data_dir / "memory"

    def _path_for(self, scope: str, entry_id: str) -> Path:
        return self.memory_dir / scope_to_dirname(scope) / f"{entry_id}.md"

    def _find_path(self, entry_id: str) -> Path:
        """按 id 全局定位文件；0 个抛 MemoryNotFoundError，多个说明数据已损坏。"""
        if not self.memory_dir.exists():
            raise MemoryNotFoundError(entry_id)
        matches = sorted(self.memory_dir.glob(f"*/{entry_id}.md"))
        if not matches:
            raise MemoryNotFoundError(entry_id)
        if len(matches) > 1:
            raise MemoryStoreError(f"id {entry_id!r} 在多个 scope 下重复出现: {matches}")
        return matches[0]

    def create(self, entry: MemoryEntry) -> MemoryEntry:
        """写入新记忆。id 已存在（任意 scope 下）时报错，fail-closed。"""
        if self.memory_dir.exists() and any(self.memory_dir.glob(f"*/{entry.id}.md")):
            raise MemoryStoreError(f"id {entry.id!r} 已存在，拒绝覆盖（create 是 fail-closed 的）")
        path = self._path_for(entry.scope, entry.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry_to_markdown(entry), encoding="utf-8")
        return entry

    def get(self, entry_id: str) -> MemoryEntry:
        path = self._find_path(entry_id)
        return entry_from_markdown(path.read_text(encoding="utf-8"), path=path)

    def update(self, entry: MemoryEntry) -> MemoryEntry:
        """更新已有条目：version 自动 +1，last_verified 置为今天。scope 不允许变更。"""
        existing = self.get(entry.id)
        if entry.scope != existing.scope:
            raise MemoryStoreError(
                f"不允许通过 update 变更 scope（{existing.scope!r} -> {entry.scope!r}），"
                "请 forget 后重新 create"
            )
        updated = entry.model_copy(
            update={"version": existing.version + 1, "last_verified": date.today()}
        )
        path = self._find_path(entry.id)
        path.write_text(entry_to_markdown(updated), encoding="utf-8")
        return updated

    def delete(self, entry_id: str) -> None:
        self._find_path(entry_id).unlink()

    def increment_retrieval_count(self, entry_id: str) -> MemoryEntry:
        """检索命中计数 +1（M4a）。只改 retrieval_count，不动 version/last_verified。"""
        path = self._find_path(entry_id)
        entry = entry_from_markdown(path.read_text(encoding="utf-8"), path=path)
        updated = entry.model_copy(update={"retrieval_count": entry.retrieval_count + 1})
        path.write_text(entry_to_markdown(updated), encoding="utf-8")
        return updated

    def list(self, scope: str | None = None) -> list[MemoryEntry]:
        """列出条目；scope=None 时列出全部。按 id 排序保证顺序稳定。"""
        if not self.memory_dir.exists():
            return []
        if scope is not None:
            dirs = [self.memory_dir / scope_to_dirname(scope)]
        else:
            dirs = sorted(p for p in self.memory_dir.iterdir() if p.is_dir())
        entries = []
        for d in dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                entries.append(entry_from_markdown(f.read_text(encoding="utf-8"), path=f))
        return sorted(entries, key=lambda e: e.id)

    def iter_all(self):
        """逐条产出全部记忆（供 rebuild 索引等批处理场景使用）。"""
        yield from self.list(scope=None)
