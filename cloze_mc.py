"""Multiple-choice cloze exercise generation.

Distractor sources (configurable):
  - ai:        AI generates context-appropriate, semantically close distractors
  - today:     Same-POS words from today's learning list
  - collection: Same-POS words from the entire deck

When multiple sources are enabled, candidates are pooled and shuffled.
"""

import random
import re

from aqt import mw
from aqt.utils import showWarning

from .config import get_config, DEFAULT_DECK_NAME
from .generator import (
    parse_pos_from_ai_response,
    parse_form_changes,
    parse_ai_distractors,
    parse_word_definitions,
    auto_detect_word_field,
)

# Cache for collection words (per session)
_collection_word_cache = None


def reset_collection_cache():
    """Clear the collection word cache (call when notes may have changed)."""
    global _collection_word_cache
    _collection_word_cache = None


def ensure_mc_note_type():
    """Find or create the 'Cloze MCQ' note type for multiple-choice cards.

    Returns {model, sentence_field, full_sentence_field, answer_field,
             options_field, info_field} or None.
    """
    col = mw.col
    if col is None:
        return None

    # Check existing
    for name in ("Cloze MCQ", "挖空选择", "填空题选择"):
        model = col.models.by_name(name)
        if model is not None:
            fields = [f["name"] for f in model["flds"]]
            if len(fields) >= 5:
                return {
                    "model": model,
                    "sentence_field": fields[0],
                    "full_sentence_field": fields[1],
                    "answer_field": fields[2],
                    "options_field": fields[3],
                    "info_field": fields[4],
                }

    # Create new
    model = col.models.new("Cloze MCQ")
    model["type"] = 0  # Standard (non-cloze)

    field_names = ["句子", "完整句子", "答案", "选项", "信息"]
    for name in field_names:
        field = col.models.new_field(name)
        col.models.add_field(model, field)

    template = col.models.new_template("Card 1")
    template["qfmt"] = (
        '<div id="mc-data" data-answer="{{答案}}" data-options="{{选项}}"'
        ' style="display:none;"></div>\n'
        '<div class="sentence">{{句子}}</div>\n'
        '<div id="opts" class="options"></div>\n'
        '<div id="fb" class="feedback"></div>\n'
        '<script>\n'
        '(function() {\n'
        '    var data = document.getElementById("mc-data");\n'
        '    var answer = data.dataset.answer;\n'
        '    var options = data.dataset.options.split("|");\n'
        '    for (var i = options.length - 1; i > 0; i--) {\n'
        '        var j = Math.floor(Math.random() * (i + 1));\n'
        '        var t = options[i]; options[i] = options[j]; options[j] = t;\n'
        '    }\n'
        '    var div = document.getElementById("opts");\n'
        '    var html = "";\n'
        '    for (var k = 0; k < options.length; k++) {\n'
        '        html += \'<button class="opt-btn"'
        ' onclick="selectOpt(this)"'
        ' data-choice="\' + options[k] + \'">\''
        ' + options[k] + \'</button>\';\n'
        '    }\n'
        '    div.innerHTML = html;\n'
        '})();\n'
        'function selectOpt(btn) {\n'
        '    var fb = document.getElementById("fb");\n'
        '    var answer ='
        ' document.getElementById("mc-data").dataset.answer;\n'
        '    var btns = document.querySelectorAll(".opt-btn");\n'
        '    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;\n'
        '    var choice = btn.dataset.choice;\n'
        '    if (choice === answer) {\n'
        '        btn.classList.add("correct");\n'
        '        fb.innerHTML ='
        ' \'<span class="correct-msg">✓ 正确！</span>\';\n'
        '    } else {\n'
        '        btn.classList.add("wrong");\n'
        '        fb.innerHTML ='
        ' \'<span class="wrong-msg">✗ '
        '正确答案：\' + answer + \'</span>\';\n'
        '        for (var i = 0; i < btns.length; i++) {\n'
        '            if (btns[i].dataset.choice === answer)'
        ' btns[i].classList.add("correct");\n'
        '        }\n'
        '    }\n'
        '}\n'
        '</script>\n'
    )
    template["afmt"] = (
        '<div class="sentence">{{完整句子}}</div>\n'
        '<div class="info">{{信息}}</div>\n'
    )

    col.models.add_template(model, template)

    model["css"] = (
        ".card { font-family: arial; font-size: 18px; text-align: center; "
        "color: black; background-color: white; }\n"
        ".sentence { font-size: 20px; line-height: 1.8; "
        "margin-bottom: 20px; padding: 10px; }\n"
        ".options { margin-top: 12px; }\n"
        ".opt-btn { margin: 6px; padding: 10px 24px; font-size: 16px; "
        "cursor: pointer; border: 2px solid #aaa; border-radius: 8px; "
        "background: #f5f5f5; transition: 0.2s; }\n"
        ".opt-btn:hover { background: #ddd; }\n"
        ".opt-btn.correct { background: #27ae60; color: white; "
        "border-color: #27ae60; }\n"
        ".opt-btn.wrong { background: #c0392b; color: white; "
        "border-color: #c0392b; }\n"
        ".correct-msg { color: #27ae60; font-weight: bold; font-size: 16px; }\n"
        ".wrong-msg { color: #c0392b; font-weight: bold; font-size: 16px; }\n"
        ".feedback { margin-top: 12px; min-height: 24px; }\n"
        ".info { margin-top: 16px; color: #666; font-size: 14px; }\n"
    )

    col.models.add(model)
    if model["id"] == 0:
        return None
    return {
        "model": model,
        "sentence_field": "句子",
        "full_sentence_field": "完整句子",
        "answer_field": "答案",
        "options_field": "选项",
        "info_field": "信息",
    }


def extract_collection_words(config=None):
    """Extract all unique words from the entire collection."""
    global _collection_word_cache
    if _collection_word_cache is not None:
        return _collection_word_cache

    col = mw.col
    if col is None:
        return []

    if config is None:
        config = get_config()

    word_field = config.get("word_field", "")
    note_type = config.get("note_type", "")

    if not word_field:
        word_field = auto_detect_word_field(note_type)
        if not word_field:
            return []

    note_ids = col.find_notes("")
    words = set()
    for nid in note_ids:
        note = col.get_note(nid)
        if note_type and note.note_type()["name"] != note_type:
            continue
        if word_field in note:
            raw = note[word_field]
            if raw:
                w = str(raw).strip()
                if w:
                    words.add(w)

    _collection_word_cache = list(words)
    return _collection_word_cache


def _guess_pos(word):
    """Heuristic POS guess for words not covered by AI classification.

    Returns a POS label matching the AI's 名词/动词/形容词/副词 scheme,
    or "" if no guess.
    """
    w = word.lower()
    # Nouns: typical suffixes
    if re.search(r'(tion|sion|ment|ness|ity|ance|ence|hood|ship|dom|er|or'
                 r'|ist|ism|ology|graphy|ture)$', w):
        return "名词"
    # Adverbs
    if w.endswith("ly"):
        return "副词"
    # Adjectives
    if re.search(r'(ous|ive|able|ible|ful|less|al|ish|ary|ory|ic|ical|'
                 r'like|worthy)$', w):
        return "形容词"
    # Verbs: past/continuous/infinitive patterns
    if re.search(r'(ed|ing|ate|ize|ify|ise|en)$', w) and len(w) > 4:
        return "动词"
    return ""


def _pick_distractors(word, used_form, pos, sources, ai_distractors,
                      pos_pools, collection_words, used_set, all_pos):
    """Pick 3 distractor candidates from enabled sources.

    ALL candidates MUST match the target word's POS — guaranteed across
    every source.
    """
    pool = []

    # ── AI distractors: validate POS against known classification ──────
    if "ai" in sources and word in ai_distractors:
        for d in ai_distractors[word]:
            dl = d.lower()
            if dl in used_set or dl == used_form.lower():
                continue
            known_pos = all_pos.get(d, "")
            if known_pos and known_pos != pos:
                continue  # Wrong POS, skip
            pool.append(d)

    # ── Today's same-POS words: guaranteed by pos_pools ────────────────
    if "today" in sources:
        for w in pos_pools.get(pos, []):
            if w.lower() != word.lower() and w.lower() not in used_set:
                pool.append(w)

    # ── Collection words: known-POS match or heuristic guess ───────────
    if "collection" in sources:
        for w in collection_words:
            wl = w.lower()
            if wl == word.lower() or wl == used_form.lower() or wl in used_set:
                continue
            # Prefer known POS, fall back to heuristic
            known_pos = all_pos.get(w, "")
            if known_pos:
                if known_pos != pos:
                    continue
            else:
                guessed = _guess_pos(w)
                if guessed and guessed != pos:
                    continue
                # If no guess either, include as last resort
            pool.append(w)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in pool:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # Shuffle and take 3
    random.shuffle(unique)
    candidates = unique[:3]

    # If AI is the only enabled source but it returned <3, the result
    # will have fewer candidates — that's expected.
    return candidates


def _extract_pure_passage(passage_text):
    """Return the passage portion before any 【分类】 section."""
    m = re.search(r'【', passage_text)
    if m:
        return passage_text[:m.start()].strip()
    return passage_text.strip()


def _blank_word(passage, word):
    """Replace all occurrences of *word* (case-insensitive, word boundary)
    in *passage* with '___'."""
    pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
    return pattern.sub('___', passage)


def _highlight_word(passage, word):
    """Wrap all occurrences of *word* in <b> tags."""
    pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
    return pattern.sub(r'<b>\g<0></b>', passage)


def create_mc_cloze_notes(passages, config=None):
    """Create multiple-choice cloze notes from generated passages.

    One note per target word per passage. Each note has the sentence with
    the word blanked out, the correct answer, and 3 same-POS distractors.

    Returns (added, skipped, errors).
    """
    col = mw.col
    if col is None:
        return 0, 0, ["请先打开一个牌组。"]

    if config is None:
        config = get_config()

    model_info = ensure_mc_note_type()
    if model_info is None:
        return 0, 0, ["无法创建或找到挖空选择题笔记类型。"]

    deck_name = config.get("mc_deck_name", "") or DEFAULT_DECK_NAME
    deck_id = col.decks.id(deck_name)

    # Parse POS, form-change, and AI distractor info from AI responses
    all_pos = {}
    all_changes = {}
    ai_distractors = {}
    for p in passages:
        if p.get("passage"):
            all_pos.update(parse_pos_from_ai_response(p["passage"]))
            all_changes.update(parse_form_changes(p["passage"]))
            ai_distractors.update(parse_ai_distractors(p["passage"]))

    # Determine enabled distractor sources
    sources = config.get("mc_distractor_sources", ["ai", "today", "collection"])
    if not sources:
        sources = ["ai", "today", "collection"]

    # Build same-POS word pools from today's list
    pos_pools = {}
    for w, pos in all_pos.items():
        pos_pools.setdefault(pos, []).append(w)

    # Extract collection words
    collection_words = extract_collection_words(config)

    added = 0
    skipped = 0
    errors = []

    for p_idx, p in enumerate(passages):
        if not p.get("passage"):
            skipped += 1
            continue

        raw_text = p["passage"]
        words = p.get("words", [])
        pure_passage = _extract_pure_passage(raw_text)
        all_defs = parse_word_definitions(raw_text)

        for word in words:
            used_form = all_changes.get(word, word)
            pos = all_pos.get(word, "")
            used_set = set(w.lower() for w in words)

            candidates = _pick_distractors(
                word=word,
                used_form=used_form,
                pos=pos,
                sources=sources,
                ai_distractors=ai_distractors,
                pos_pools=pos_pools,
                collection_words=collection_words,
                used_set=used_set,
                all_pos=all_pos,
            )

            # Build sentence with blank
            sentence = _blank_word(pure_passage, used_form)
            full_sentence = _highlight_word(pure_passage, used_form)

            # Shuffle options
            options = [used_form] + candidates
            random.shuffle(options)
            options_str = "|".join(options)

            # Build info field
            info_parts = [f"原形: {word}"]
            def_info = all_defs.get(word, ("", ""))
            def_text = def_info[0]
            if def_text:
                info_parts.append(f"释义: {def_text}")
            if pos:
                info_parts.append(f"词性: [{pos}]")
            if word in all_changes:
                info_parts.append(f"文中形式: {used_form}")
            info = " | ".join(info_parts)

            try:
                note = col.new_note(model_info["model"])
                note[model_info["sentence_field"]] = sentence
                note[model_info["full_sentence_field"]] = full_sentence
                note[model_info["answer_field"]] = used_form
                note[model_info["options_field"]] = options_str
                note[model_info["info_field"]] = info
                col.add_note(note, deck_id)
                added += 1
            except Exception as e:
                errors.append(
                    f"短文{p_idx + 1} 单词'{word}': 创建失败 — {e}")
                skipped += 1

    return added, skipped, errors
