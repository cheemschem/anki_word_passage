"""Cloze deletion exercise generation from AI-generated passages.

Converts target words in passages into Anki {{cN::word}} cloze markers,
creating one card per unique word.
"""

import re

from aqt import mw
from .config import DEFAULT_DECK_NAME
from .generator import parse_word_definitions


def ensure_cloze_note_type():
    """Find or create the Anki 'Cloze' note type (type=1).

    Returns a dict {model, text_field, extra_field} on success, or None.
    The text_field / extra_field names are read from the actual model so
    they work regardless of Anki's UI language.
    """
    col = mw.col
    if col is None:
        return None

    # 1) Search existing models by type==1 (language-independent)
    for model in col.models.all():
        if model.get("type") == 1:
            fields = [f["name"] for f in model["flds"]]
            if len(fields) < 1:
                continue
            return {
                "model": model,
                "text_field": fields[0],
                "extra_field": fields[1] if len(fields) > 1 else "",
            }

    # 2) Try by name for English / Chinese
    for name in ("Cloze", "填空题", "挖空", "cloze"):
        model = col.models.by_name(name)
        if model is not None:
            fields = [f["name"] for f in model["flds"]]
            if len(fields) < 1:
                continue
            return {
                "model": model,
                "text_field": fields[0],
                "extra_field": fields[1] if len(fields) > 1 else "",
            }

    # 3) Create a fresh cloze model
    model = col.models.new("Cloze")
    model["type"] = 1

    for name in ["Text", "Back Extra"]:
        field = col.models.new_field(name)
        col.models.add_field(model, field)

    template = col.models.new_template("Cloze")
    template["qfmt"] = "{{cloze:Text}}"
    template["afmt"] = "{{cloze:Text}}<br>{{Back Extra}}"
    col.models.add_template(model, template)

    model["css"] = (
        ".card { font-family: arial; font-size: 20px; text-align: center; "
        "color: black; background-color: white; }\n"
        ".cloze { font-weight: bold; color: blue; }\n"
    )

    col.models.add(model)
    return {"model": model, "text_field": "Text", "extra_field": "Back Extra"}


def passage_to_cloze(passage_text, target_words):
    """Wrap each unique target word in {{cN::word}} cloze markers.

    Returns (cloze_text, found_words, missing_words).
    """
    if not passage_text or not target_words:
        return passage_text, [], list(target_words)

    # Deduplicate case-insensitively, preserving first-seen order
    seen = {}
    unique_words = []
    for w in target_words:
        key = w.lower()
        if key not in seen:
            seen[key] = True
            unique_words.append(w)

    # Sort by length descending so longer matches take priority
    unique_words.sort(key=len, reverse=True)
    word_to_num = {w.lower(): i for i, w in enumerate(unique_words, start=1)}

    escaped = [re.escape(w) for w in unique_words]
    pattern = r'\b(?:' + '|'.join(escaped) + r')\b'

    found = set()

    def _replace(m):
        matched = m.group(0)
        n = word_to_num.get(matched.lower(), 1)
        found.add(matched.lower())
        return f'{{{{c{n}::{matched}}}}}'

    result = re.sub(pattern, _replace, passage_text, flags=re.IGNORECASE)

    found_words = [w for w in unique_words if w.lower() in found]
    missing_words = [w for w in unique_words if w.lower() not in found]

    return result, found_words, missing_words


def add_cloze_notes(passages, deck_name=DEFAULT_DECK_NAME):
    """Create Anki cloze notes from generated passages.

    Returns:
        (added_count, skipped_count, errors: list[str])
    """
    col = mw.col
    if col is None:
        return 0, 0, ["请先打开一个牌组。"]

    model_info = ensure_cloze_note_type()
    if model_info is None:
        return 0, 0, ["无法创建或找到挖空(Cloze)笔记类型。"]

    model = model_info["model"]
    text_field = model_info["text_field"]
    extra_field = model_info["extra_field"]

    deck_id = col.decks.id(deck_name)

    added = 0
    skipped = 0
    errors = []

    for i, p in enumerate(passages):
        if not p.get("passage"):
            skipped += 1
            errors.append(f"短文 {i + 1}: 内容为空")
            continue

        passage_text = p["passage"]
        words = p.get("words", [])

        cloze_text, found, missing = passage_to_cloze(passage_text, words)

        if not found:
            skipped += 1
            errors.append(
                f"短文 {i + 1}: 目标单词均未在短文中找到 "
                f"({', '.join(words[:5])}{'...' if len(words) > 5 else ''})")
            continue

        all_defs = parse_word_definitions(passage_text)
        back_lines = ["目标单词:"]
        for w in found:
            def_info = all_defs.get(w, ("", ""))
            def_text, pos_text = def_info[0], def_info[1]
            if def_text and pos_text:
                back_lines.append(f"  {w} - {def_text} [{pos_text}]")
            elif def_text:
                back_lines.append(f"  {w} - {def_text}")
            else:
                back_lines.append(f"  {w}")
        if missing:
            back_lines.append("未找到: " + ", ".join(missing))
        back_extra = "\n".join(back_lines)

        try:
            note = col.new_note(model)
            note[text_field] = cloze_text
            if extra_field:
                note[extra_field] = back_extra
            col.add_note(note, deck_id)
            added += 1
        except Exception as e:
            skipped += 1
            errors.append(f"短文 {i + 1}: 创建笔记失败 — {e}")

    return added, skipped, errors
