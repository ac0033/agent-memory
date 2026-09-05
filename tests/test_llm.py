"""llm.py 测试：协议满足、缺 key 报错、JSON 解析重试与 fail-closed。"""

import json

import pytest

from agent_memory.config import Settings
from agent_memory.llm import LLMClient, LLMError, OpenAILLMClient, ValidatingLLMClient


class FakeLLM:
    """返回固定内容的 fake，验证满足 LLMClient 协议。"""

    def complete(self, system: str, user: str) -> str:
        return "ok"

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        return {"ok": True}


def test_fake_satisfies_protocol():
    assert isinstance(FakeLLM(), LLMClient)


def test_missing_api_key_fails_closed():
    with pytest.raises(LLMError, match="api_key"):
        OpenAILLMClient(api_key=None)


def test_from_settings_missing_key_fails_closed():
    with pytest.raises(LLMError):
        OpenAILLMClient.from_settings(Settings())


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """脚本化的 chat.completions.create：按队列依次吐出内容。"""

    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _FakeResponse(self._contents.pop(0))


class _FakeOpenAI:
    def __init__(self, contents):
        completions = _FakeCompletions(contents)
        self.chat = type("Chat", (), {"completions": completions})()
        self.completions = completions


def _make_client(monkeypatch, contents):
    fake = _FakeOpenAI(contents)
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: fake)
    client = OpenAILLMClient(api_key="sk-test1234567890abcdef")
    return client, fake.completions


def test_complete_json_parses_dict(monkeypatch):
    client, _ = _make_client(monkeypatch, [json.dumps({"memories": []})])
    assert client.complete_json("s", "u", "schema") == {"memories": []}


def test_complete_json_retries_once_on_bad_json(monkeypatch):
    client, completions = _make_client(monkeypatch, ["not json", '{"a": 1}'])
    assert client.complete_json("s", "u", "schema") == {"a": 1}
    assert completions.calls == 2


def test_complete_json_fail_closed_after_retry(monkeypatch):
    client, completions = _make_client(monkeypatch, ["bad", "still bad"])
    with pytest.raises(LLMError, match="解析失败"):
        client.complete_json("s", "u", "schema")
    assert completions.calls == 2


def test_complete_json_rejects_non_dict(monkeypatch):
    client, _ = _make_client(monkeypatch, ['[1, 2]', '{"ok": true}'])
    # 第一次返回数组（不是 object），重试后返回合法 object
    assert client.complete_json("s", "u", "schema") == {"ok": True}


def test_validating_client_rejects_truthy_string_boolean():
    class BadBoundaryLLM(FakeLLM):
        def complete_json(self, system, user, schema_description):
            return {"pass": "false", "reason": "wrong type"}

    with pytest.raises(LLMError, match="pass 必须是 boolean"):
        ValidatingLLMClient(BadBoundaryLLM()).complete_json(
            "s", "u", '{"pass": true/false, "reason": "..."}'
        )


def test_api_exception_is_wrapped_as_llm_error(monkeypatch):
    client, completions = _make_client(monkeypatch, ["unused"])

    def fail(**_kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(completions, "create", fail)
    with pytest.raises(LLMError, match="ConnectionError"):
        client.complete("s", "u")


# ---------------------------------------------------------------- 磁盘缓存


def test_complete_cache_hit_skips_api(monkeypatch, tmp_path):
    """相同 model+system+user 第二次调用直接命中缓存，不再发 API 请求。"""
    client, completions = _make_client(monkeypatch, ["hello"])
    client.cache_dir = tmp_path
    assert client.complete("s", "u") == "hello"
    assert client.complete("s", "u") == "hello"
    assert completions.calls == 1


def test_complete_no_cache_by_default(monkeypatch):
    """不传 cache_dir 时不写缓存，每次都发 API 请求。"""
    client, completions = _make_client(monkeypatch, ["a", "b"])
    assert client.complete("s", "u") == "a"
    assert client.complete("s", "u") == "b"
    assert completions.calls == 2


def test_complete_json_cache_hit_skips_api(monkeypatch, tmp_path):
    client, completions = _make_client(monkeypatch, ['{"a": 1}'])
    client.cache_dir = tmp_path
    assert client.complete_json("s", "u", "schema") == {"a": 1}
    assert client.complete_json("s", "u", "schema") == {"a": 1}
    assert completions.calls == 1


def test_cache_miss_on_different_model(monkeypatch, tmp_path):
    """换模型后同一提示词不命中（key 含 model 名）。"""
    client, completions = _make_client(monkeypatch, ["m1", "m2"])
    client.cache_dir = tmp_path
    assert client.complete("s", "u") == "m1"
    client.model = "deepseek-v4-flash"
    assert client.complete("s", "u") == "m2"
    assert completions.calls == 2


def test_cache_miss_on_different_prompt(monkeypatch, tmp_path):
    client, completions = _make_client(monkeypatch, ["x", "y"])
    client.cache_dir = tmp_path
    assert client.complete("s", "u1") == "x"
    assert client.complete("s", "u2") == "y"
    assert completions.calls == 2


def test_cache_concurrent_writes_no_corruption(monkeypatch, tmp_path):
    """多线程并发写缓存不留半截文件：写完后所有缓存文件都是合法 JSON。"""
    from concurrent.futures import ThreadPoolExecutor

    client, _ = _make_client(monkeypatch, [f"r{i}" for i in range(8)])
    client.cache_dir = tmp_path
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: client.complete("s", f"u{i}"), range(8)))
    for f in tmp_path.glob("*.json"):
        json.loads(f.read_text(encoding="utf-8"))  # 不抛异常即合法
    assert not list(tmp_path.glob("*.tmp"))
