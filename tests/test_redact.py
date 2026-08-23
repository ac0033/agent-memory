"""redact 脱敏规则：每条规则的命中与替换，以及不误伤正常文本。"""

import pytest

from agent_memory.long_term.ingest.redact import redact

PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA7\n+bWovpJ5Rfakekeymaterial
-----END RSA PRIVATE KEY-----"""

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
)

HEX32 = "d94f3f016ae679c3008de268209132f2"


class TestEachRule:
    @pytest.mark.parametrize(
        "text, hit_type",
        [
            (f"我的私钥是：\n{PEM}\n请保存好", "pem_private_key"),
            ("请求头用 Authorization: Bearer abcdef123456.token_value", "bearer_token"),
            (f"这个 JWT 过期了：{JWT}", "jwt"),
            ("AWS key: AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
            ("配置里写 api_key=abcdef1234567890", "api_key"),
            ("配置里写 apiKey: xyz789abc123", "api_key"),
            ("OpenAI 的 key 是 sk-projAbCdEfGh1234567890", "sk_key"),
            ("联系邮箱 zhangsan@example.com", "email"),
            (f"token 是 {HEX32}", "hex_secret"),
            ("Cookie: session=abc123; theme=dark", "cookie"),
            ("Set-Cookie: sid=xyz; Path=/; HttpOnly", "cookie"),
        ],
    )
    def test_rule_hits(self, text, hit_type):
        redacted, hits = redact(text)
        assert hit_type in hits
        assert f"[REDACTED:{hit_type}]" in redacted

    def test_pem_body_fully_removed(self):
        redacted, hits = redact(PEM)
        assert "fakekeymaterial" not in redacted
        assert hits == ["pem_private_key"]

    def test_multiple_hits_all_reported(self):
        text = f"Bearer {JWT} 和邮箱 a@b.com"
        _, hits = redact(text)
        assert "bearer_token" in hits
        assert "jwt" in hits  # 命中检测在原始文本上做，不被 bearer 替换掩盖
        assert "email" in hits

    def test_hits_deduplicated(self):
        _, hits = redact("a@b.com 和 c@d.com")
        assert hits == ["email"]


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "text",
        [
            "用户的开发机是 Windows 11，终端用 Git Bash，命令要写 Unix 语法。",
            "本项目 dev server 固定使用 8765 端口，数据库是 SQLite，文件在 data/dev.db。",
            "commit message 用 Conventional Commits，例如 feat(auth): add token refresh。",
            "def fetch(url, api_version=3):\n    return requests.get(url)\n",
            "依赖写进 pyproject.toml，用 uv sync 安装。",
            "sklearn 的版本是 1.5.2，和 sk- 前缀没关系。",  # 短 sk- 不误伤
            "短 hex 如 abc123 或 commit sha 前 7 位 d94f3f0 不算敏感。",
        ],
    )
    def test_clean_text_untouched(self, text):
        redacted, hits = redact(text)
        assert hits == []
        assert redacted == text
