"""Tests for label sanitizing: redaction (secrets/PII) and Figma normalization."""

from __future__ import annotations

import pytest
from diagram_codebase.sanitize import REDACTED, Redactor, sanitize_label

# Fake credentials, built so they match the patterns but are obviously not real.
FAKE = {
    "aws_key": "AKIAABCDEFGHIJKLMNOP",
    "github_token": "ghp_" + "a1B2c3D4e5" * 4,
    "github_pat": "github_pat_" + "11ABCDEFG0" * 3,
    "slack_token": "xoxb-1234567890-fakefakefake",
    "google_api_key": "AIza" + "SyFAKE0123456789abcdefghijklmnopq",
    "stripe_key": "sk_live_" + "FAKE0123456789abcd",
    "stripe_pk": "pk_test_" + "FAKE0123456789abcd",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJlZmFrZQ",
}


@pytest.mark.parametrize(
    ("key", "category"),
    [
        ("aws_key", "aws_key"),
        ("github_token", "github_token"),
        ("github_pat", "github_token"),
        ("slack_token", "slack_token"),
        ("google_api_key", "google_api_key"),
        ("stripe_key", "stripe_key"),
        ("stripe_pk", "stripe_key"),
        ("jwt", "jwt"),
    ],
)
def test_token_patterns_are_redacted(key, category):
    clean, cats = sanitize_label(f"uses {FAKE[key]} here")
    assert FAKE[key] not in clean
    assert clean == f"uses {REDACTED} here"
    assert category in cats


def test_pem_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX\n-----END RSA PRIVATE KEY-----"
    clean, cats = sanitize_label(f"key: {pem} done")
    assert "MIIB" not in clean and "PRIVATE" not in clean
    assert cats == ["private_key"]
    clean, _ = sanitize_label("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1")  # truncated block
    assert clean == REDACTED


@pytest.mark.parametrize(
    "text",
    [
        "password = hunter2",
        "db_password=s3cr3t!",
        "API_KEY: 'abc123xyz'",
        "api-key = q9w8e7r6",
        "client_secret := topsecretvalue",
        'auth_token = "fake-token-value"',
    ],
)
def test_credential_assignments_keep_key_drop_value(text):
    clean, cats = sanitize_label(text)
    assert cats == ["credential_assignment"]
    assert clean.endswith(REDACTED)
    assert (
        clean.split()[0].rstrip(":=").lower().startswith(text.split()[0].rstrip(":=").lower()[:3])
    )


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=1000",
        "token=self.token",
        "password = os.environ",
        "token = get_token()",
        "secret = None",
        "token: refreshed on login",
        "Tokenizer: splits words",
        "password reset flow",
    ],
)
def test_credential_like_code_is_not_redacted(text):
    assert sanitize_label(text) == (text, [])


def test_url_userinfo_credentials():
    clean, cats = sanitize_label("connect postgres://admin:pw123@db.internal:5432/app")
    assert clean == f"connect postgres://{REDACTED}@db.internal:5432/app"
    assert cats == ["url_credentials"]
    assert sanitize_label("https://example.com/a?b=c") == ("https://example.com/a?b=c", [])


def test_email():
    clean, cats = sanitize_label("owner alice.smith+x@example.co.uk, ping")
    assert clean == f"owner {REDACTED}, ping"
    assert cats == ["email"]
    assert sanitize_label("@app.route('/x')")[1] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/alice/proj/app/service.py", "service.py"),
        ("/Users/bob/code/main.go", "main.go"),
        ("C:\\Users\\carol\\src\\app.cs", "app.cs"),
        ("C:/Users/carol/src/app.cs", "app.cs"),
        ("/root/deploy.sh", "deploy.sh"),
        ("/home/alice", REDACTED),
        ("/root", REDACTED),
    ],
)
def test_home_paths_keep_basename(text, expected):
    clean, cats = sanitize_label(f"file {text}")
    assert clean == f"file {expected}"
    assert cats == ["home_path"]


@pytest.mark.parametrize(
    "text", ["/api/todos/{}", "/users/{id}", "/etc/nginx/nginx.conf", "~/projects"]
)
def test_non_home_paths_survive(text):
    assert sanitize_label(text) == (text, [])


def test_private_ips_only():
    clean, cats = sanitize_label(
        "10.1.2.3 172.16.0.9 192.168.1.1 169.254.1.1 127.0.0.1 0.0.0.0 8.8.8.8 172.32.0.1"
    )
    assert (
        clean == f"{REDACTED} {REDACTED} {REDACTED} {REDACTED} 127.0.0.1 0.0.0.0 8.8.8.8 172.32.0.1"
    )
    assert cats == ["private_ip"]
    assert sanitize_label("version 1.2.3.4 and v10.0.0.1 and 999.1.1.1") == (
        "version 1.2.3.4 and v10.0.0.1 and 999.1.1.1",
        [],
    )


def test_high_entropy_and_hex():
    assert sanitize_label("sha " + "deadbeef" * 5)[0] == f"sha {REDACTED}"
    clean, cats = sanitize_label("blob Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg")
    assert clean == f"blob {REDACTED}" and cats == ["high_entropy"]


@pytest.mark.parametrize(
    "text",
    [
        "create_publisher",
        "/api/todos/{}",
        "O(n)",
        "O(n log n)",
        "InventoryServiceRestockHandlerFactory",
        "test_create_publisher_2_with_options",
        "skills/diagram_codebase/scripts/v2/handler_3",
        "fn:app/service.py:InventoryService.restock",
        "GET /api/v1/items/{}",
        "rclcpp::Node::create_subscription",
        "123e4567-e89b-12d3-a456-426614174000",
        "List<int>",
        "a -> b",
        "日本語のラベル",
    ],
)
def test_ordinary_identifiers_unchanged(text):
    assert sanitize_label(text) == (text, [])


def test_normalization():
    assert (
        sanitize_label("Hi 🚀 there ❤️ 👨\u200d👩\u200d👧 1\ufe0f\u20e3 🇺🇸 x")[0] == "Hi there 1 x"
    )
    assert sanitize_label("a<br>b <b>bold</b> <span class='x'>s</span>")[0] == "a b bold s"
    assert sanitize_label("line1\\nline2\nline3\r\nline4\tend")[0] == "line1 line2 line3 line4 end"
    assert sanitize_label("`code`   spaced\x00\x07out")[0] == "code spaced out"
    assert sanitize_label("bidi\u202eevil\u200bzero")[0] == "bidievilzero"
    assert sanitize_label(None) == ("", [])
    assert sanitize_label(42) == ("42", [])


def test_redactor_counts():
    r = Redactor()
    assert r("mail a@example.com and b@example.com") == f"mail {REDACTED} and {REDACTED}"
    assert r(f"key {FAKE['aws_key']}") == f"key {REDACTED}"
    assert r("plain") == "plain"
    report = r.report()
    assert report == {"total": 3, "labels": 2, "by_category": {"aws_key": 1, "email": 2}}
    assert "example.com" not in str(report)
