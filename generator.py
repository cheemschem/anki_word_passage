"""Core logic: fetch today's due words and generate passages."""

import os
import re
from datetime import datetime

from aqt import mw
from aqt.qt import QThread, pyqtSignal
from aqt.utils import showInfo, showWarning, tooltip

from .config import get_config, get_default, DEFAULT_DECK_NAME
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
    try:
        models = mw.col.models.all()
    except Exception:
        return None
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
            "分组列出所有目标单词。格式: "
            "'单词原形 - 中文释义"
            " (文中形式: 实际出现的单词, 干扰项: 语义相近词1, 语义相近词2, 语义相近词3)'\n"
            "干扰项要求：与目标词语义相近、词性相同、可填入句子中语法通顺但语义错误。"
        )

    if include_translation:
        prompt += "\n\n请附带整篇短文的中文翻译。"

    if supplement.strip():
        prompt += "\n\n【补充要求】" + supplement.strip()

    return prompt


def parse_form_changes(response_text):
    """Extract form-change annotations from AI response.

    Parses lines like:  run - 跑 (文中形式: ran)
    Returns dict: {original_word: form_used_in_passage}
    """
    changes = {}
    # [^,)]+ stops at comma (before 干扰项) or closing paren
    pattern = re.compile(
        r'(.+?)\s*[-—]\s*.+?\(文中形式[：:]\s*([^,)]+)\)')
    for line in response_text.splitlines():
        m = pattern.search(line)
        if m:
            original = m.group(1).strip()
            used = m.group(2).strip()
            if original.lower() != used.lower():
                changes[original] = used
    return changes


def parse_ai_distractors(response_text):
    """Extract AI-suggested distractors from AI response.

    Parses lines like:
        ambiguous - 模糊的 (干扰项: obvious, clear, straightforward)
        run - 跑 (文中形式: ran, 干扰项: walk, jog, sprint)

    Returns dict: {word: [distractor1, distractor2, distractor3]}
    """
    distractor_map = {}
    # Case 1: (文中形式: xxx, 干扰项: a, b, c) — form change with distractors
    pat_with_form = re.compile(
        r'(.+?)\s*[-—]\s*.+?'
        r'\(文中形式[：:]\s*[^,)]+,\s*干扰项[：:]\s*([^)]+)\)')
    # Case 2: (干扰项: a, b, c) — distractors only
    pat_simple = re.compile(
        r'(.+?)\s*[-—]\s*.+?'
        r'\(干扰项[：:]\s*([^)]+)\)')

    for line in response_text.splitlines():
        m = pat_with_form.search(line)
        if not m:
            m = pat_simple.search(line)
        if m:
            word = m.group(1).strip()
            raw = m.group(2).strip()
            distractors = [d.strip() for d in re.split(r'[,，;；]', raw) if d.strip()]
            if distractors:
                distractor_map[word] = distractors[:3]
    return distractor_map


def parse_pos_from_ai_response(response_text):
    """Parse POS categories from AI response.

    Parses the AI output like:
        【名词】
        apple - 苹果
        dog - 狗 (文中形式: dogs)
        【动词】
        run - 跑

    Returns dict: {word: pos_name}  where pos_name is "名词"/"动词"/etc.
    """
    pos_map = {}
    current_pos = None
    pos_header = re.compile(r'【(.+?)】')
    word_line = re.compile(r'^(.+?)\s*[-—]')

    for line in response_text.splitlines():
        line = line.strip()
        if not line:
            continue
        hm = pos_header.match(line)
        if hm:
            current_pos = hm.group(1)
            continue
        if current_pos:
            wm = word_line.match(line)
            if wm:
                word = wm.group(1).strip()
                # Strip the "(文中形式: xxx)" suffix if present
                word = re.sub(r'\s*\(文中形式[：:][^)]*\)', '', word).strip()
                if word:
                    pos_map[word] = current_pos

    return pos_map


def parse_word_definitions(response_text):
    """Parse word → (definition, pos) from AI response.

    Parses lines like:
        run - 跑 (文中形式: ran)
        apple - 苹果

    Returns dict: {word: (definition_string, pos_category)}
    """
    defs = {}
    current_pos = None
    pos_header = re.compile(r'【(.+?)】')
    word_line = re.compile(r'^(.+?)\s*[-—]\s*(.+?)(?:\s*\(|$)')

    for line in response_text.splitlines():
        line = line.strip()
        if not line:
            continue
        hm = pos_header.match(line)
        if hm:
            current_pos = hm.group(1)
            continue
        if current_pos:
            wm = word_line.match(line)
            if wm:
                word = wm.group(1).strip()
                # Clean non-word chars
                word = re.sub(r'\s*\([^)]*\)', '', word).strip()
                raw_def = wm.group(2).strip()
                # Strip trailing parens that contain form/distractor annotations
                defn = re.sub(r'\s*\([^)]*\)', '', raw_def).strip()
                if word and defn:
                    defs[word] = (defn, current_pos)
    return defs


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

    supplement = passage_dlg.supplement_edit.toPlainText().strip()
    passage_dlg._worker = _GenerateWorker(
        all_word_texts, api_params, prompt_params, supplement)
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

def _ensure_date_dir(save_path=None):
    if save_path is None:
        config = get_config()
        save_path = config.get("save_path", "")
    if not save_path:
        return None, None
    now = datetime.now()
    ym_dir = os.path.join(save_path, now.strftime("%Y-%m"))
    day_dir = os.path.join(ym_dir, now.strftime("%m-%d"))
    try:
        os.makedirs(day_dir, exist_ok=True)
    except OSError:
        return None, None
    return ym_dir, day_dir


def _make_stamped_filename(prefix, ext, now=None):
    """Generate filename: {prefix}_{YYYY-MM-DD_HH-MM-SS}.{ext}"""
    if now is None:
        now = datetime.now()
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{prefix}_{ts}.{ext}"


def save_passage_as_txt(passage_text, word_texts, form_changes=None,
                        prefix="passage", save_path=None):
    _, day_dir = _ensure_date_dir(save_path)
    if not day_dir:
        return None
    now = datetime.now()
    filename = _make_stamped_filename(prefix, "txt", now)
    filepath = os.path.join(day_dir, filename)
    word_list = "\n".join(f"  • {w}" for w in word_texts)
    parts = [
        "今日单词短文",
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"包含单词（{len(word_texts)} 个）：\n{word_list}",
    ]
    if form_changes:
        change_lines = "\n".join(
            f"  {orig} → {used}" for orig, used in form_changes.items())
        parts.append(f"单词形式变动：\n{change_lines}")

    # Format passage text: 15 words per line
    words = passage_text.split()
    passage_lines = []
    for i in range(0, len(words), 15):
        passage_lines.append(" ".join(words[i:i + 15]))
    formatted_passage = "\n".join(passage_lines)

    parts.append(f"{'─' * 40}\n\n{formatted_passage}\n")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
    except (IOError, OSError):
        return None
    return filepath


def save_passage_as_pdf(passage_text, word_texts, form_changes=None,
                        prefix="passage", save_path=None):
    _, day_dir = _ensure_date_dir(save_path)
    if not day_dir:
        return None
    now = datetime.now()
    filename = _make_stamped_filename(prefix, "pdf", now)
    filepath = os.path.join(day_dir, filename)

    word_list = ", ".join(word_texts)
    change_html = ""
    if form_changes:
        change_lines = "".join(
            f"<li>{orig} → {used}</li>"
            for orig, used in form_changes.items())
        change_html = (
            f"<p style='color:#666;font-size:13px;'>"
            f"单词形式变动：<ul>{change_lines}</ul></p>")

    body = passage_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    html = (
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;"
        "font-size:16px;line-height:1.8;padding:40px;}"
        "h2{color:#333;border-bottom:2px solid #4a90d9;padding-bottom:8px;}"
        "</style></head><body>"
        f"<h2>今日单词短文</h2>"
        f"<p style='color:#888;font-size:12px;'>"
        f"生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}<br>"
        f"包含单词（{len(word_texts)} 个）：{word_list}</p>"
        f"{change_html}"
        f"<hr style='border:0;border-top:1px solid #ddd;margin:16px 0;'>"
        f"<p style='font-size:17px;'>{body}</p>"
        "</body></html>"
    )

    try:
        from PyQt6.QtPrintSupport import QPrinter
        from PyQt6.QtGui import QTextDocument, QPageSize
    except ImportError:
        try:
            from aqt.qt import QPrinter, QTextDocument, QPageSize
        except ImportError:
            showWarning("PDF 生成不可用：缺少 PyQt 打印支持。")
            return None

    try:
        doc = QTextDocument()
        doc.setHtml(html)
        printer = QPrinter()
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(filepath)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        doc.print_(printer)
        return filepath
    except Exception as e:
        showWarning(f"PDF 生成失败: {e}")
        return None


def save_all_passages(passages, options=None):
    """Unified save: TXT + PDF + cloze companion.

    Args:
        passages: List of {passage, words} dicts.
        options: dict with keys: save_txt, save_pdf, save_cloze, save_mc_cloze,
                 prefix, save_path, cloze_deck_name, mc_deck_name.

    Returns dict: {txt: [paths], pdf: [paths], cloze: (added, skipped, errors),
                   mc_cloze: (added, skipped, errors)}
    """
    if options is None:
        config = get_config()
        options = {
            "save_txt": config.get("save_txt", True),
            "save_pdf": config.get("save_pdf", False),
            "save_cloze": config.get("save_cloze", True),
            "save_mc_cloze": config.get("save_mc_cloze", True),
            "prefix": config.get("save_prefix", "passage"),
            "save_path": config.get("save_path", ""),
            "cloze_deck_name": config.get("cloze_deck_name", DEFAULT_DECK_NAME),
            "mc_deck_name": config.get("mc_deck_name", DEFAULT_DECK_NAME),
        }

    result = {"txt": [], "pdf": [], "cloze": (0, 0, []),
              "mc_cloze": (0, 0, [])}

    for p in passages:
        if not p.get("passage"):
            continue

        passage_text = p["passage"]
        words = p.get("words", [])
        changes = parse_form_changes(passage_text)

        if options.get("save_txt"):
            fp = save_passage_as_txt(passage_text, words, changes,
                                     options.get("prefix", "passage"),
                                     options.get("save_path", ""))
            if fp:
                result["txt"].append(fp)

        if options.get("save_pdf"):
            fp = save_passage_as_pdf(passage_text, words, changes,
                                     options.get("prefix", "passage"),
                                     options.get("save_path", ""))
            if fp:
                result["pdf"].append(fp)

    # Create Anki cloze notes (one batch call per source)
    if options.get("save_cloze"):
        from . import cloze
        result["cloze"] = cloze.add_cloze_notes(
            passages, options.get("cloze_deck_name", DEFAULT_DECK_NAME))

    if options.get("save_mc_cloze"):
        from . import cloze_mc
        mc_cfg = {
            "mc_deck_name": options.get("mc_deck_name", DEFAULT_DECK_NAME),
            "mc_distractor_sources": options.get(
                "mc_distractor_sources", ["ai", "today", "collection"]),
            "word_field": options.get("word_field", ""),
            "note_type": options.get("note_type", ""),
        }
        result["mc_cloze"] = cloze_mc.create_mc_cloze_notes(passages, mc_cfg)

    return result


