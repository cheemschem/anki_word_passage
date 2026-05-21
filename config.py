"""Configuration management for the AI Word Passage add-on.

Config is stored via Anki's addonManager API (survives add-on reinstalls)
with a local JSON file as fallback.
"""

import json
import os

ADDON_NAME = "anki_word_passage"
DEFAULT_DECK_NAME = "默认"
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_CONFIG_DIR, "user_config.json")

DEFAULT_CONFIG = {
    # ── API ──────────────────────────────────────────────────────────────
    "api_endpoint": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_tokens": 4000,
    # Model presets with endpoints
    "model_presets": {
        "DeepSeek (深度求索)": {
            "endpoint": "https://api.deepseek.com/v1",
            "models": ["deepseek-chat", "deepseek-reasoner"],
        },
        "Qwen (阿里云通义千问)": {
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        },
        "Kimi (月之暗面)": {
            "endpoint": "https://api.moonshot.cn/v1",
            "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        },
        "GLM (智谱 AI)": {
            "endpoint": "https://open.bigmodel.cn/api/paas/v4",
            "models": ["glm-4-plus", "glm-4-0520", "glm-4-flash"],
        },
        "Hunyuan (腾讯混元)": {
            "endpoint": "https://api.hunyuan.cloud.tencent.com/v1",
            "models": ["hunyuan-pro", "hunyuan-standard", "hunyuan-lite"],
        },
        "ERNIE (百度文心一言)": {
            "endpoint": "https://qianfan.baidubce.com/v2",
            "models": ["ernie-4.0-8k-latest", "ernie-3.5-8k", "ernie-speed-128k"],
        },
        "Doubao (字节豆包)": {
            "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
            "models": ["doubao-pro-128k"],
        },
        "Yi (零一万物)": {
            "endpoint": "https://api.lingyiwanwu.com/v1",
            "models": ["yi-large", "yi-medium", "yi-spark"],
        },
        "Step (阶跃星辰)": {
            "endpoint": "https://api.stepfun.com/v1",
            "models": ["step-2-16k", "step-1-128k", "step-1-flash"],
        },
        "MiniMax (稀宇 abab)": {
            "endpoint": "https://api.minimax.chat/v1",
            "models": ["abab6.5s-chat", "abab6.5t-chat"],
        },
        "Baichuan (百川智能)": {
            "endpoint": "https://api.baichuan-ai.com/v1",
            "models": ["Baichuan4", "Baichuan3-Turbo"],
        },
        "MiMo (小米)": {
            "endpoint": "https://api.xiaomimimo.com/v1",
            "models": ["mimo-v2.5-pro", "mimo-v2-pro", "mimo-v2-omni", "mimo-v2-flash"],
        },
    },
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
        "确保所有目标单词都出现在短文中。你可以根据上下文需要改变单词的形式"
        "（如过去式、复数、-ing形式、比较级等），使短文更加自然流畅。"
        "短文要有趣、易于理解，帮助记忆单词。"
    ),
    # ── Cloze ─────────────────────────────────────────────────────────────
    "cloze_deck_name": DEFAULT_DECK_NAME,
    "mc_deck_name": DEFAULT_DECK_NAME,
    "mc_distractor_sources": ["ai", "today", "collection"],
    # ── Behaviour ────────────────────────────────────────────────────────
    # ── Save ───────────────────────────────────────────────────────────────
    "save_txt": True,
    "save_pdf": False,
    "save_cloze": True,
    "save_mc_cloze": True,
    "auto_generate": False,
    "save_path": "",
    "save_prefix": "passage",
}


def _read_local_json():
    """Read config from local JSON file (fallback)."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _read_addon_manager():
    """Read config from Anki's addonManager (survives reinstalls)."""
    try:
        from aqt import mw
        if mw is not None:
            cfg = mw.addonManager.getConfig(ADDON_NAME)
            if cfg:
                return cfg
    except Exception:
        pass
    return {}


def _read_saved():
    """Read saved config — addonManager first, then local JSON."""
    cfg = _read_addon_manager()
    if cfg:
        return cfg
    return _read_local_json()


def get_config():
    saved = _read_saved()
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    return merged


def save_config(config):
    clean = {k: v for k, v in config.items() if k in DEFAULT_CONFIG}

    # Primary: save via Anki's addonManager (profile folder, survives reinstalls)
    try:
        from aqt import mw
        if mw is not None:
            mw.addonManager.writeConfig(ADDON_NAME, clean)
    except Exception:
        pass

    # Fallback: local JSON
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def get_default(key):
    return DEFAULT_CONFIG.get(key)
