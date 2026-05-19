"""Configuration management for the AI Word Passage add-on.

Config is stored as JSON alongside this file (user_config.json),
avoiding dependency on Anki's addonManager API which varies by version.
"""

import json
import os

ADDON_NAME = "anki_word_passage"
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_CONFIG_DIR, "user_config.json")

DEFAULT_CONFIG = {
    # ── API ──────────────────────────────────────────────────────────────
    "api_endpoint": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_tokens": 2000,
    # ── Word selection ───────────────────────────────────────────────────
    "word_field": "",
    "note_type": "",
    "total_words_limit": 50,
    # ── Generation ───────────────────────────────────────────────────────
    "words_per_passage": 10,
    "output_words": 200,
    "include_translation": False,
    "classify_vocab": True,
    # ── Prompt ───────────────────────────────────────────────────────────
    "system_prompt": (
        "你是一个专业的英语老师。请用以下单词编写一篇自然流畅的英语短文（约{output_words}词），"
        "确保所有目标单词都出现在短文中。短文要有趣、易于理解，帮助记忆单词。"
    ),
    # ── Behaviour ────────────────────────────────────────────────────────
    "auto_generate": False,
    "save_path": "",
}


def _read_saved():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def get_config():
    saved = _read_saved()
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    return merged


def save_config(config):
    clean = {k: v for k, v in config.items() if k in DEFAULT_CONFIG}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def get_default(key):
    return DEFAULT_CONFIG.get(key)
