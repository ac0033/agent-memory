"""工作记忆存储（data/working，M7a）。

目录约定：<data_dir>/working/<scope 目录名>.md，每个 scope 一份文档
（scope 的目录名映射复用长期记忆层的 scope_to_dirname，":" -> "__"）。

序列化：YAML frontmatter 存 WorkingMemory 的全量 dump（mode="json"），
正文渲染成人可读文本——正文仅供人翻看，读取只认 frontmatter。

fail-closed：frontmatter 缺失/非法直接抛异常，不做静默降级
（与长期记忆层 markdown_store 同一风格）。
"""

import re
from datetime import datetime
from pathlib import Path

import yaml

from agent_memory.long_term.store.markdown_store import scope_to_dirname
from agent_memory.working.models import WorkingMemory

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class WorkingMemoryStoreError(ValueError):
    """工作记忆读写错误（frontmatter 非法等），fail-closed 直接抛出。"""


class WorkingMemoryNotFoundError(KeyError):
    """按 scope 找不到工作记忆时抛出。"""


def wm_to_markdown(wm: WorkingMemory) -> str:
    """WorkingMemory -> 带 YAML frontmatter 的 Markdown 文本。

    frontmatter 是机器可读的唯一事实来源；正文是人可读的渲染，仅供翻看。
    """
    meta = wm.model_dump(mode="json")
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{frontmatter}---\n\n{_render_body(wm)}"


def _render_body(wm: WorkingMemory) -> str:
    """人可读正文（不用于读取，不参与任何逻辑）。"""
    lines = [f"# 工作记忆：{wm.scope}", ""]
    if wm.goal:
        lines += ["## 目标", "", wm.goal, ""]
    if wm.todos:
        lines += ["## 待办", ""]
        for t in wm.todos:
            mark = "x" if t.status == "done" else " "
            lines.append(f"- [{mark}] {t.content}")
        lines.append("")
    if wm.decisions:
        lines += ["## 已确认决策", ""]
        lines += [f"- {d}" for d in wm.decisions]
        lines.append("")
    if wm.variables:
        lines += ["## 变量", ""]
        lines += [f"- {k}: {v}" for k, v in wm.variables.items()]
        lines.append("")
    if wm.notes:
        lines += ["## 备注（未决问题与阻塞）", ""]
        lines += [f"- {n}" for n in wm.notes]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def wm_from_markdown(text: str, *, path: Path | None = None) -> WorkingMemory:
    """Markdown 文本 -> WorkingMemory。只认 frontmatter；缺失或非法直接报错。"""
    where = f"（{path}）" if path else ""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise WorkingMemoryStoreError(f"工作记忆文件缺少合法的 YAML frontmatter{where}")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise WorkingMemoryStoreError(f"frontmatter YAML 解析失败{where}: {e}") from e
    if not isinstance(meta, dict):
        raise WorkingMemoryStoreError(f"frontmatter 必须是 YAML 映射{where}")
    try:
        return WorkingMemory.model_validate(meta)
    except ValueError as e:
        raise WorkingMemoryStoreError(f"frontmatter 字段校验失败{where}: {e}") from e


class WorkingMemoryStore:
    """data/working 层的读写。写入语义是全量替换，没有部分更新。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.working_dir = self.data_dir / "working"

    def _path_for(self, scope: str) -> Path:
        return self.working_dir / f"{scope_to_dirname(scope)}.md"

    def read(self, scope: str) -> WorkingMemory | None:
        """读取一个 scope 的工作记忆；文件不存在返回 None（不是错误）。"""
        path = self._path_for(scope)
        if not path.exists():
            return None
        return wm_from_markdown(path.read_text(encoding="utf-8"), path=path)

    def write(self, wm: WorkingMemory) -> WorkingMemory:
        """全量写入。已存在则 version 在旧值上 +1、updated_at 置为当前时间；
        不存在则按传入内容创建（version 应为 1）。"""
        path = self._path_for(wm.scope)
        existing = self.read(wm.scope)
        if existing is not None:
            wm = wm.model_copy(
                update={"version": existing.version + 1, "updated_at": datetime.now()}
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wm_to_markdown(wm), encoding="utf-8")
        return wm

    def delete(self, scope: str) -> None:
        """删除一个 scope 的工作记忆；不存在时抛 WorkingMemoryNotFoundError。"""
        path = self._path_for(scope)
        if not path.exists():
            raise WorkingMemoryNotFoundError(scope)
        path.unlink()
