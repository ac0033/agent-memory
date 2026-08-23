"""review_queue.py 读侧测试：列出待办、读取单项、删除已处理项。"""

import pytest

from agent_memory.long_term.ingest.review_queue import (
    delete_review_item,
    list_review_queue,
    load_review_item,
    write_review_queue,
    write_review_queue_raw,
)


def test_list_empty_when_no_queue_dir(tmp_path):
    assert list_review_queue(tmp_path) == []


def test_list_returns_entries_sorted_by_queued_at(tmp_path, entry_factory):
    e1 = entry_factory(entry_id="entry-a", confidence="low")
    e2 = entry_factory(entry_id="entry-b", confidence="low")
    write_review_queue([e1], tmp_path, reason="confidence=low，规则门分流")
    write_review_queue([e2], tmp_path, reason="confidence=low，规则门分流")

    items = list_review_queue(tmp_path)
    assert len(items) == 2
    assert all(not it["unreadable"] for it in items)
    assert {it["entry"]["id"] for it in items} == {"entry-a", "entry-b"}
    assert all(it["reason"] for it in items)
    assert items[0]["queued_at"] <= items[1]["queued_at"]
    # evolution/ 子目录的整理提案不在列出范围内
    assert all(it["file"].endswith(".yaml") for it in items)


def test_list_marks_corrupt_file_unreadable(tmp_path, entry_factory):
    write_review_queue(
        [entry_factory(entry_id="entry-a", confidence="low")], tmp_path, reason="测试"
    )
    queue_dir = tmp_path / "review_queue"
    (queue_dir / "broken-file.yaml").write_text("{{ 不是合法 yaml: [", encoding="utf-8")

    items = list_review_queue(tmp_path)
    assert len(items) == 2
    corrupt = [it for it in items if it["unreadable"]]
    assert len(corrupt) == 1
    assert corrupt[0]["file"] == "broken-file.yaml"
    assert corrupt[0]["entry"] is None


def test_load_review_item_roundtrip(tmp_path, entry_factory):
    files = write_review_queue(
        [entry_factory(entry_id="entry-a", confidence="low")], tmp_path, reason="测试原因"
    )
    payload = load_review_item(tmp_path, files[0].name)
    assert payload["entry"]["id"] == "entry-a"
    assert payload["reason"] == "测试原因"


def test_load_raw_record_item(tmp_path):
    files = write_review_queue_raw(
        [({"id": "Bad ID!", "content": "x"}, "id 非法")], tmp_path, reason="蒸馏产出非法"
    )
    payload = load_review_item(tmp_path, files[0].name)
    assert "entry" not in payload
    assert payload["raw_record"] == {"id": "Bad ID!", "content": "x"}


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError, match="非法队列文件名"):
        load_review_item(tmp_path, "../outside.yaml")
    with pytest.raises(ValueError, match="非法队列文件名"):
        load_review_item(tmp_path, "evolution/proposal.yaml")
    with pytest.raises(ValueError, match="非法队列文件名"):
        delete_review_item(tmp_path, "no-extension")


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_review_item(tmp_path, "not-exist.yaml")
    with pytest.raises(FileNotFoundError):
        delete_review_item(tmp_path, "not-exist.yaml")


def test_delete_review_item(tmp_path, entry_factory):
    files = write_review_queue(
        [entry_factory(entry_id="entry-a", confidence="low")], tmp_path, reason="测试"
    )
    delete_review_item(tmp_path, files[0].name)
    assert list_review_queue(tmp_path) == []
