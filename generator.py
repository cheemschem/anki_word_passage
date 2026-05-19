"""Core logic: fetch today's due words and generate passages."""

import os
from datetime import datetime

from aqt import mw
from aqt.qt import QThread, pyqtSignal
from aqt.utils import showInfo, showWarning, tooltip

from .config import get_config, get_default
from .api import WordPassageAPI, APIError


WORD_FIELD_CANDIDATES = [
    "Front", "front", "单词", "Word", "word",
    "正面", "Expression", "expression", "Text", "text",
    "Term", "term", "Kanji", "kanji", "Vocabulary", "vocabulary",
    "Spanish", "French", "German",
]


# ---------------------------------------------------------------------------
# Word extraction
# ---------------------------------------------------------------------------

def auto_detect_word_field(note_type=None):
    models = mw.col.models.all()
    if note_type:
        models = [m for m in models if m["name"] == note_type]
    if not models:
        return None
    for model in models:
        field_names = [f["name"] for f in model["flds"]]
        for candidate in WORD_FIELD_CANDIDATES:
            if candidate in field_names:
                return candidate
    if models[0]["flds"]:
        return models[0]["flds"][0]["name"]
    return None


def get_todays_words(config):
    """Fetch today's words — review (due) + new (queue order, limited)."""
    col = mw.col
    if col is None:
        showWarning("请先打开一个牌组。")
        return []

    note_type = config.get("note_type", "")
    word_field = config.get("word_field", "")
    total_limit = config.get("total_words_limit", 50)

    if not word_field:
        word_field = auto_detect_word_field(note_type)
        if word_field is None:
            showWarning("无法自动检测单词字段，请在设置中手动指定。")
            return []

    words_seen = set()
    words = []

    # Learning cards — rated:1 introduced:1
    learning_cids = col.find_cards("rated:1 introduced:1")

    # Review cards — rated:1 but not in learning
    review_cids = col.find_cards("rated:1 -introduced:1")

    def _extract(cid_list, source):
        for cid in cid_list:
            card = col.get_card(cid)
            note = card.note()
            if note_type and note.note_type()["name"] != note_type:
                continue
            if word_field in note:
                raw = note[word_field]
                if raw is None:
                    continue
                word = str(raw).strip()
                if word and word not in words_seen:
                    words_seen.add(word)
                    words.append((word, source))

    _extract(learning_cids, "学习")
    _extract(review_cids, "复习")

    return words


# ---------------------------------------------------------------------------
# Prompt builder  (pure — no Anki refs)
# ---------------------------------------------------------------------------

def _build_system_prompt(base_prompt, output_words, classify_vocab,
                         include_translation, supplement):
    """Assemble the final system prompt. All args are plain Python types."""
    prompt = base_prompt.replace("{output_words}", str(output_words))

    if classify_vocab:
        prompt += (
            "\n\n请在短文后按【名词】【动词】【形容词】【副词】【其他】"
            "分组列出所有目标单词及其中文释义。"
        )

    if include_translation:
        prompt += "\n\n请附带整篇短文的中文翻译。"

    if supplement.strip():
        prompt += "\n\n【补充要求】" + supplement.strip()

    return prompt


def generate_passage(word_strings, api_params, prompt_params, supplement):
    """Call the API. All args are plain Python — thread-safe."""
    api = WordPassageAPI(api_params)
    system_prompt = _build_system_prompt(supplement=supplement, **prompt_params)
    return api.generate_passage(word_strings, system_prompt)


# ---------------------------------------------------------------------------
# Background worker  (zero Anki refs)
# ---------------------------------------------------------------------------

class _GenerateWorker(QThread):
    """Generates passages for all word groups in a background thread.

    All inputs are pre-extracted plain Python types — no Anki / Qt refs.
    """
    finished = pyqtSignal(object)

    def __init__(self, word_string_groups, api_params, prompt_params,
                 supplement=""):
        super().__init__()
        self._word_string_groups = word_string_groups  # list of list of str
        self._api_params = api_params                  # dict of str → str/int
        self._prompt_params = prompt_params            # dict of str → str/int/bool
        self._supplement = supplement

    def run(self):
        results = []
        for group in self._word_string_groups:
            try:
                text = generate_passage(
                    group, self._api_params, self._prompt_params,
                    self._supplement)
                results.append({"passage": text, "words": group, "error": None})
            except APIError as e:
                results.append({"passage": None, "words": group,
                                "error": str(e)})
            except Exception as e:
                results.append({"passage": None, "words": group,
                                "error": f"未知错误: {e}"})
        self.finished.emit(results)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def generate_and_show(parent=None):
    """Entry point: get words → confirm → generate all passages."""
    config = get_config()

    if not config.get("api_key"):
        showWarning("请先在设置中配置 API Key（工具 → AI 单词短文设置）。")
        return

    words = get_todays_words(config)
    if not words:
        showInfo("今天没有待学习的单词（新学 + 复习均为空）。")
        return

    _show_confirm_and_generate(words, config, parent)


def auto_generate_if_enabled():
    """Called before study — auto-generate if configured."""
    config = get_config()
    if not config.get("auto_generate"):
        return
    if not config.get("api_key"):
        return
    words = get_todays_words(config)
    if not words:
        return
    _show_confirm_and_generate(words, config)


def _show_confirm_and_generate(words, config, parent=None):
    """Confirm → split into groups → passage dialog → worker → result popup."""
    from .ui import WordConfirmDialog, PassageDialog, GenerationResultDialog

    total_limit = config.get("total_words_limit", 50)

    dlg = WordConfirmDialog(words, config, parent or mw)
    if dlg.exec() == 0:
        return

    selected, temp_cfg = dlg.result()
    if not selected:
        showInfo("没有选中任何单词。")
        return

    # Merge temp overrides into a working config
    work_cfg = {**config, **temp_cfg}

    # Split into groups  (篇数 = ceil(total / per_passage))
    word_groups = _split_into_groups(selected, work_cfg)
    if not word_groups:
        showInfo("单词不足以生成短文。")
        return

    all_word_texts = [
        [w[0] if isinstance(w, tuple) else w for w in g]
        for g in word_groups
    ]

    # Pre-extract plain data for the worker (zero Anki refs)
    api_params = {
        "api_endpoint": work_cfg["api_endpoint"],
        "api_key": work_cfg["api_key"],
        "model": work_cfg["model"],
        "temperature": work_cfg["temperature"],
        "max_tokens": work_cfg["max_tokens"],
    }
    prompt_params = {
        "base_prompt": work_cfg.get("system_prompt")
                       or get_default("system_prompt"),
        "output_words": work_cfg.get("output_words", 200),
        "classify_vocab": work_cfg.get("classify_vocab", True),
        "include_translation": work_cfg.get("include_translation", False),
    }

    # Show PassageDialog in loading state
    passage_dlg = PassageDialog(None, all_word_texts, work_cfg, parent or mw)
    passage_dlg.show()

    passage_dlg._worker = _GenerateWorker(
        all_word_texts, api_params, prompt_params)
    passage_dlg._worker.finished.connect(passage_dlg._on_worker_done)
    passage_dlg._worker.start()


def _split_into_groups(selected, config):
    """Split selected words evenly into groups."""
    words_per = config.get("words_per_passage", 10)
    if words_per <= 0:
        words_per = 10
    groups = []
    i = 0
    while i < len(selected):
        group = selected[i:i + words_per]
        groups.append(group)
        i += words_per
    return groups


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def _ensure_date_dir():
    config = get_config()
    save_path = config.get("save_path", "")
    if not save_path:
        showWarning("请先在设置中配置保存路径。")
        return None
    date_dir = os.path.join(save_path, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(date_dir, exist_ok=True)
    return date_dir


def save_passage_as_txt(passage_text, word_texts):
    date_dir = _ensure_date_dir()
    if not date_dir:
        return False
    timestamp = datetime.now().strftime("%H%M%S%f")[:-3]
    filename = f"passage_{timestamp}.txt"
    filepath = os.path.join(date_dir, filename)
    word_list = "\n".join(f"  • {w}" for w in word_texts)
    content = (
        f"今日单词短文\n"
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"包含单词（{len(word_texts)} 个）：\n{word_list}\n"
        f"{'─' * 40}\n\n"
        f"{passage_text}\n"
    )
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


def save_all_passages(passages):
    saved = []
    for p in passages:
        if p["passage"] is None:
            continue
        path = save_passage_as_txt(p["passage"], p["words"])
        if path:
            saved.append(path)
    return saved


def save_as_anki_note(passage_text, word_texts):
    showInfo("Anki 笔记保存功能开发中，请先用纯文本保存。")
    return False
