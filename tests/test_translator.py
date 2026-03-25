import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processor.translator import ClaudeTranslator


def test_sanitize_json_keeps_object_keys_intact() -> None:
    raw = """{
  "title_zh": "标题",
  "body_zh": "正文",
  "tags": ["A", "B", "C"]
}"""

    sanitized = ClaudeTranslator._sanitize_json(raw)
    data = json.loads(sanitized)

    assert data["title_zh"] == "标题"
    assert data["body_zh"] == "正文"
    assert data["tags"] == ["A", "B", "C"]


def test_sanitize_json_escapes_bare_quotes_inside_values() -> None:
    raw = """{
  "title_zh": "标题",
  "body_zh": "这里提到 "auto mode" 和 "agents"",
  "tags": ["A", "B", "C"]
}"""

    sanitized = ClaudeTranslator._sanitize_json(raw)
    data = json.loads(sanitized)

    assert data["body_zh"] == '这里提到 "auto mode" 和 "agents"'
