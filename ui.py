"""Qt UI components for the AI Word Passage add-on."""

import re
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QTextEdit, QPushButton, QCheckBox, QGroupBox, QTabWidget,
    QApplication, QThread, pyqtSignal, QFileDialog, QFrame,
    QComboBox,
)
from aqt.utils import showWarning, showInfo, tooltip

from .config import get_config, save_config, DEFAULT_DECK_NAME
from .generator import (
    _GenerateWorker,
    save_passage_as_txt, save_all_passages,
)
from .api import test_connection, fetch_models


def _get_deck_names():
    """Get all deck names from the Anki collection."""
    try:
        from aqt import mw
        if mw and mw.col:
            return mw.col.decks.all_names()
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════════════════════════════════════
# Word-confirmation dialog
# ═══════════════════════════════════════════════════════════════════════════

class WordConfirmDialog(QDialog):
    """Word checkboxes + collapsible generation settings (temp overrides)."""

    def __init__(self, words, config, parent=None):
        super().__init__(parent)
        self._words = words
        self._config = config
        self._learning_checks = []
        self._review_checks = []
        self._build_ui()
        self.setWindowTitle("确认生成单词")
        self.resize(640, 600)

    def _build_ui(self):
        from aqt.qt import QScrollArea, QWidget, QVBoxLayout as VLayout

        layout = QVBoxLayout(self)

        learning_words = [(w, s) for w, s in self._words if s == "学习"]
        review_words = [(w, s) for w, s in self._words if s == "复习"]

        # Top counter
        self._counter_label = QLabel()
        self._counter_label.setWordWrap(True)
        layout.addWidget(self._counter_label)

        # Two word columns
        cols_layout = QHBoxLayout()
        cols_layout.addWidget(
            self._make_column("学习", learning_words, "#2980b9",
                              self._learning_checks))
        cols_layout.addWidget(
            self._make_column("复习", review_words, "#c0392b",
                              self._review_checks))
        layout.addLayout(cols_layout, 1)

        # Toggle row
        toggle_layout = QHBoxLayout()
        all_btn = QPushButton("全选")
        all_btn.clicked.connect(lambda: self._set_all(True))
        toggle_layout.addWidget(all_btn)
        none_btn = QPushButton("取消全选")
        none_btn.clicked.connect(lambda: self._set_all(False))
        toggle_layout.addWidget(none_btn)
        toggle_layout.addStretch()
        layout.addLayout(toggle_layout)

        # ── Generation settings (collapsible) ──────────────────────────
        gen_group = QGroupBox("生成设置（本次临时，不覆盖全局默认）")
        gen_group.setCheckable(True)
        gen_group.setChecked(False)
        gen_form = QFormLayout(gen_group)

        self.total_spin = QSpinBox()
        self.total_spin.setRange(5, 500)
        self.total_spin.setValue(self._config.get("total_words_limit", 50))
        self.total_spin.valueChanged.connect(self._update_counter)
        gen_form.addRow("总输入词汇上限:", self.total_spin)

        self.per_passage_spin = QSpinBox()
        self.per_passage_spin.setRange(3, 50)
        self.per_passage_spin.setValue(self._config.get("words_per_passage", 10))
        gen_form.addRow("每篇短文输入单词数:", self.per_passage_spin)

        self.translation_check = QCheckBox("生成短文中文翻译")
        self.translation_check.setChecked(
            self._config.get("include_translation", False))
        gen_form.addRow("", self.translation_check)

        self.classify_check = QCheckBox("按词性分类目标单词")
        self.classify_check.setChecked(
            self._config.get("classify_vocab", True))
        gen_form.addRow("", self.classify_check)

        # Estimated passage count (read-only)
        self._est_label = QLabel()
        gen_form.addRow("预计篇数:", self._est_label)
        self._update_estimate()

        # Recalc estimate when per_passage changes
        self.per_passage_spin.valueChanged.connect(lambda _: self._update_estimate())

        layout.addWidget(gen_group)

        # Confirm / Cancel
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        confirm_btn = QPushButton("生成短文")
        confirm_btn.setDefault(True)
        confirm_btn.clicked.connect(self.accept)
        btn_layout.addWidget(confirm_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        self._update_counter()

    def _make_column(self, title, words, color, check_list):
        from aqt.qt import QVBoxLayout as VLayout, QScrollArea, QWidget

        group = QGroupBox(f"{title} ({len(words)})")
        group.setStyleSheet(f"QGroupBox{{color:{color};font-weight:bold;}}")
        vl = VLayout(group)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        inner = VLayout(container)
        inner.setSpacing(2)

        for word, _source in words:
            cb = QCheckBox(word)
            cb.setStyleSheet(f"color:{color};")
            cb.toggled.connect(self._update_counter)
            check_list.append(cb)
            inner.addWidget(cb)

        inner.addStretch()
        scroll.setWidget(container)
        vl.addWidget(scroll)
        return group

    # ── pre-select ─────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        if any(cb.isChecked() for cb in self._learning_checks + self._review_checks):
            return
        remaining = self.total_spin.value()
        for cb in self._learning_checks:
            if remaining <= 0:
                break
            cb.setChecked(True)
            remaining -= 1
        for cb in self._review_checks:
            if remaining <= 0:
                break
            cb.setChecked(True)
            remaining -= 1
        self._update_counter()

    def _set_all(self, checked):
        remaining = self.total_spin.value() if checked else 0
        all_cbs = self._learning_checks + self._review_checks
        for cb in all_cbs:
            if checked and remaining <= 0:
                cb.setChecked(False)
                continue
            cb.setChecked(checked)
            if checked:
                remaining -= 1
        self._update_counter()

    # ── counters ───────────────────────────────────────────────────────

    def _update_counter(self, *_):
        sel = sum(1 for cb in self._learning_checks + self._review_checks
                  if cb.isChecked())
        limit = self.total_spin.value()
        self._counter_label.setText(
            f"共 <b>{len(self._words)}</b> 个今日单词，"
            f"已选 <b style='color:#c0392b;'>{sel}</b> / 上限 {limit}")
        self._update_estimate()

    def _update_estimate(self):
        sel = sum(1 for cb in self._learning_checks + self._review_checks
                  if cb.isChecked())
        per = self.per_passage_spin.value()
        if per <= 0:
            per = 10
        n = (sel + per - 1) // per
        self._est_label.setText(f"{n} 篇（{sel} ÷ {per}）")

    # ── result ─────────────────────────────────────────────────────────

    def selected_words(self):
        """Return checked words as (word, source) tuples."""
        learning_words = [(w, s) for w, s in self._words if s == "学习"]
        review_words = [(w, s) for w, s in self._words if s == "复习"]
        result = []
        for (w, s), cb in zip(learning_words, self._learning_checks):
            if cb.isChecked():
                result.append((w, s))
        for (w, s), cb in zip(review_words, self._review_checks):
            if cb.isChecked():
                result.append((w, s))
        return result

    def result(self):
        """Return (selected_words, temp_overrides_dict)."""
        overrides = {
            "total_words_limit": self.total_spin.value(),
            "words_per_passage": self.per_passage_spin.value(),
            "include_translation": self.translation_check.isChecked(),
            "classify_vocab": self.classify_check.isChecked(),
        }
        return self.selected_words(), overrides


# ═══════════════════════════════════════════════════════════════════════════
# Background worker for connection test
# ═══════════════════════════════════════════════════════════════════════════

class _TestWorker(QThread):
    finished = pyqtSignal(bool, str)

    def __init__(self, config):
        super().__init__()
        self._config = config

    def run(self):
        success, msg = test_connection(self._config)
        self.finished.emit(success, msg)


class _FetchModelsWorker(QThread):
    finished = pyqtSignal(bool, object)

    def __init__(self, endpoint, api_key):
        super().__init__()
        self._endpoint = endpoint
        self._api_key = api_key

    def run(self):
        success, data = fetch_models(self._endpoint, self._api_key)
        self.finished.emit(success, data)


# ═══════════════════════════════════════════════════════════════════════════
# Settings dialog
# ═══════════════════════════════════════════════════════════════════════════

class SettingsDialog(QDialog):
    """Global defaults for API, word selection, generation & save."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = get_config()
        self._build_ui()
        self._load_config()
        self.setWindowTitle("AI 单词短文设置")
        self.setMinimumSize(540, 400)
        self.resize(700, 800)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Scroll area for settings content
        from aqt.qt import QScrollArea, QWidget
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        content = QVBoxLayout(container)
        content.setContentsMargins(0, 0, 0, 0)

        # ── API ────────────────────────────────────────────────────────
        api_group = QGroupBox("API 设置")
        api_form = QFormLayout(api_group)

        # Endpoint: vendor preset drives endpoint
        ep_row = QHBoxLayout()
        self.endpoint_preset = QComboBox()
        self._preset_data = {}  # label -> {endpoint, models}
        self._preset_reverse = {}  # endpoint -> index in combo
        self.endpoint_preset.addItem("（选择厂商预设）")
        presets = self.config.get("model_presets", {})
        for vendor, info in presets.items():
            label = f"{vendor}"
            self.endpoint_preset.addItem(label)
            self._preset_data[label] = info
            self._preset_reverse[info["endpoint"]] = (
                self.endpoint_preset.count() - 1)
        # Custom endpoint option
        self._custom_idx = self.endpoint_preset.count()
        self.endpoint_preset.addItem("自定义 (Custom)")
        self.endpoint_preset.currentIndexChanged.connect(self._on_preset_changed)
        ep_row.addWidget(self.endpoint_preset, 1)

        # Custom endpoint edit (hidden unless "自定义" is selected)
        self.custom_ep_edit = QLineEdit()
        self.custom_ep_edit.setPlaceholderText("https://your-api-endpoint.com/v1")
        self.custom_ep_edit.setVisible(False)
        ep_row.addWidget(self.custom_ep_edit, 2)
        api_form.addRow("Endpoint:", ep_row)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-...")
        api_form.addRow("API Key:", self.key_edit)

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        all_models = []
        for info in presets.values():
            all_models.extend(info["models"])
        self.model_combo.addItems(all_models)
        self.model_combo.setCurrentText("deepseek-chat")
        model_row.addWidget(self.model_combo, 1)
        self.fetch_models_btn = QPushButton("获取模型")
        self.fetch_models_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self.fetch_models_btn)
        api_form.addRow("Model:", model_row)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setDecimals(1)
        api_form.addRow("Temperature:", self.temp_spin)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(100, 1000000)
        self.max_tokens_spin.setSingleStep(1000)
        self.max_tokens_spin.setValue(100000)
        api_form.addRow("Max Tokens:", self.max_tokens_spin)

        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._on_test)
        api_form.addRow("", self.test_btn)

        content.addWidget(api_group)

        # ── Word selection ─────────────────────────────────────────────
        field_group = QGroupBox("单词字段设置")
        field_form = QFormLayout(field_group)

        self.field_edit = QLineEdit()
        self.field_edit.setPlaceholderText("留空自动检测")
        field_form.addRow("单词字段名:", self.field_edit)

        self.note_type_edit = QLineEdit()
        self.note_type_edit.setPlaceholderText("留空则匹配所有笔记类型")
        field_form.addRow("笔记类型:", self.note_type_edit)

        self.total_words_spin = QSpinBox()
        self.total_words_spin.setRange(5, 500)
        self.total_words_spin.setValue(50)
        field_form.addRow("默认总输入词汇上限:", self.total_words_spin)

        content.addWidget(field_group)

        # ── Generation defaults ────────────────────────────────────────
        gen_group = QGroupBox("生成设置（全局默认，确认界面可临时覆盖）")
        gen_form = QFormLayout(gen_group)

        self.words_per_spin = QSpinBox()
        self.words_per_spin.setRange(3, 50)
        self.words_per_spin.setValue(10)
        gen_form.addRow("默认每篇短文输入单词数:", self.words_per_spin)

        self.output_words_spin = QSpinBox()
        self.output_words_spin.setRange(50, 3000)
        self.output_words_spin.setSingleStep(50)
        self.output_words_spin.setValue(200)
        gen_form.addRow("每篇输出词汇数:", self.output_words_spin)

        self.translation_check = QCheckBox("生成短文中文翻译")
        gen_form.addRow("", self.translation_check)

        self.classify_check = QCheckBox("按词性分类目标单词（名词/动词/形容词等）")
        gen_form.addRow("", self.classify_check)

        content.addWidget(gen_group)

        # ── Save path ──────────────────────────────────────────────────
        save_group = QGroupBox("短文保存")
        save_form = QFormLayout(save_group)

        path_row = QHBoxLayout()
        self.save_path_edit = QLineEdit()
        self.save_path_edit.setPlaceholderText("选择文件夹...")
        path_row.addWidget(self.save_path_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._on_browse_save_path)
        path_row.addWidget(browse_btn)
        save_form.addRow("保存路径:", path_row)

        content.addWidget(save_group)

        # ── Cloze ──────────────────────────────────────────────────────
        cloze_group = QGroupBox("挖空练习设置")
        cloze_form = QFormLayout(cloze_group)

        self.cloze_deck_combo = QComboBox()
        self.cloze_deck_combo.setEditable(True)
        self.cloze_deck_combo.addItem(DEFAULT_DECK_NAME)
        cloze_form.addRow("目标牌组:", self.cloze_deck_combo)

        content.addWidget(cloze_group)

        # ── MC Cloze ───────────────────────────────────────────────────
        mc_cloze_group = QGroupBox("挖空选择题设置")
        mc_cloze_form = QFormLayout(mc_cloze_group)

        self.mc_deck_combo = QComboBox()
        self.mc_deck_combo.setEditable(True)
        self.mc_deck_combo.addItem(DEFAULT_DECK_NAME)
        mc_cloze_form.addRow("目标牌组:", self.mc_deck_combo)

        self.mc_source_ai = QCheckBox("AI 生成（语义相近，上下文匹配）")
        self.mc_source_ai.setChecked(True)
        mc_cloze_form.addRow("干扰项来源:", self.mc_source_ai)

        self.mc_source_today = QCheckBox("当天学习单词（同词性）")
        self.mc_source_today.setChecked(True)
        mc_cloze_form.addRow("", self.mc_source_today)

        self.mc_source_collection = QCheckBox("牌库单词（同词性随机）")
        self.mc_source_collection.setChecked(True)
        mc_cloze_form.addRow("", self.mc_source_collection)

        content.addWidget(mc_cloze_group)

        # ── Prompt ─────────────────────────────────────────────────────
        prompt_group = QGroupBox("提示词设置")
        prompt_layout = QVBoxLayout(prompt_group)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "自定义系统提示词...（可用 {output_words} 占位）")
        self.prompt_edit.setMaximumHeight(100)
        prompt_layout.addWidget(self.prompt_edit)
        content.addWidget(prompt_group)

        # ── Auto ───────────────────────────────────────────────────────
        self.auto_check = QCheckBox("进入学习时自动生成今日单词短文")
        content.addWidget(self.auto_check)

        # Add scroll area to main layout
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        # ── Buttons ────────────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        save_btn.setDefault(True)
        btn_layout.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    # ── load / save ────────────────────────────────────────────────────

    def _load_config(self):
        # Endpoint: find matching preset or switch to custom
        saved_ep = self.config.get("api_endpoint", "")
        if saved_ep and saved_ep in self._preset_reverse:
            self.endpoint_preset.setCurrentIndex(self._preset_reverse[saved_ep])
        elif saved_ep:
            self.endpoint_preset.setCurrentIndex(self._custom_idx)
            self.custom_ep_edit.setText(saved_ep)
        self.key_edit.setText(self.config.get("api_key", ""))
        model = self.config.get("model", "")
        if model:
            self.model_combo.setCurrentText(model)
        self.temp_spin.setValue(self.config.get("temperature", 0.7))
        self.max_tokens_spin.setValue(self.config.get("max_tokens", 100000))
        self.field_edit.setText(self.config.get("word_field", ""))
        self.note_type_edit.setText(self.config.get("note_type", ""))
        self.total_words_spin.setValue(
            self.config.get("total_words_limit", 50))
        self.words_per_spin.setValue(
            self.config.get("words_per_passage", 10))
        self.output_words_spin.setValue(
            self.config.get("output_words", 200))
        self.translation_check.setChecked(
            self.config.get("include_translation", False))
        self.classify_check.setChecked(
            self.config.get("classify_vocab", True))
        self.save_path_edit.setText(self.config.get("save_path", ""))
        # Populate deck combos from existing Anki decks
        for combo in (self.cloze_deck_combo, self.mc_deck_combo):
            existing = _get_deck_names()
            for name in existing:
                if combo.findText(name) < 0:
                    combo.addItem(name)
        self.cloze_deck_combo.setCurrentText(
            self.config.get("cloze_deck_name", DEFAULT_DECK_NAME))
        self.mc_deck_combo.setCurrentText(
            self.config.get("mc_deck_name", DEFAULT_DECK_NAME))
        sources = self.config.get("mc_distractor_sources", ["ai", "today", "collection"])
        self.mc_source_ai.setChecked("ai" in sources)
        self.mc_source_today.setChecked("today" in sources)
        self.mc_source_collection.setChecked("collection" in sources)
        self.prompt_edit.setPlainText(self.config.get("system_prompt", ""))
        self.auto_check.setChecked(self.config.get("auto_generate", False))

    def _on_save(self):
        idx = self.endpoint_preset.currentIndex()
        if idx <= 0:
            ep = self.config.get("api_endpoint", "")
        elif idx == self._custom_idx:
            ep = self.custom_ep_edit.text().strip()
        else:
            label = self.endpoint_preset.currentText()
            info = self._preset_data.get(label, {})
            ep = info.get("endpoint", "")
        self.config["api_endpoint"] = ep
        self.config["api_key"] = self.key_edit.text().strip()
        self.config["model"] = self.model_combo.currentText().strip()
        self.config["temperature"] = self.temp_spin.value()
        self.config["max_tokens"] = self.max_tokens_spin.value()
        self.config["word_field"] = self.field_edit.text().strip()
        self.config["note_type"] = self.note_type_edit.text().strip()
        self.config["total_words_limit"] = self.total_words_spin.value()
        self.config["words_per_passage"] = self.words_per_spin.value()
        self.config["output_words"] = self.output_words_spin.value()
        self.config["include_translation"] = self.translation_check.isChecked()
        self.config["classify_vocab"] = self.classify_check.isChecked()
        self.config["save_path"] = self.save_path_edit.text().strip()
        self.config["cloze_deck_name"] = self.cloze_deck_combo.currentText().strip() or DEFAULT_DECK_NAME
        self.config["mc_deck_name"] = self.mc_deck_combo.currentText().strip() or DEFAULT_DECK_NAME
        sources = []
        if self.mc_source_ai.isChecked():
            sources.append("ai")
        if self.mc_source_today.isChecked():
            sources.append("today")
        if self.mc_source_collection.isChecked():
            sources.append("collection")
        self.config["mc_distractor_sources"] = sources
        self.config["system_prompt"] = self.prompt_edit.toPlainText().strip()
        self.config["auto_generate"] = self.auto_check.isChecked()

        try:
            save_config(self.config)
        except Exception as e:
            showWarning(f"保存配置失败: {e}")
            return

        showInfo("配置已保存。")
        self.accept()

    # ── preset ────────────────────────────────────────────────────────

    def _on_preset_changed(self, idx):
        if idx == self._custom_idx:
            self.custom_ep_edit.setVisible(True)
            return
        self.custom_ep_edit.setVisible(False)
        if idx <= 0:
            return
        label = self.endpoint_preset.currentText()
        info = self._preset_data.get(label)
        if info:
            self.model_combo.clear()
            self.model_combo.addItems(info["models"])
            if info["models"]:
                self.model_combo.setCurrentText(info["models"][0])

    # ── test / browse ──────────────────────────────────────────────────

    def _on_test(self):
        temp_config = {
            "api_endpoint": self._current_endpoint(),
            "api_key": self.key_edit.text().strip(),
            "model": (self.model_combo.currentText().strip()
                      or self.config["model"]),
        }
        if not temp_config["api_key"]:
            showWarning("请先输入 API Key。")
            return

        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中...")
        self._worker = _TestWorker(temp_config)
        self._worker.finished.connect(self._on_test_result)
        self._worker.start()

    def _on_test_result(self, success, message):
        self.test_btn.setEnabled(True)
        self.test_btn.setText("测试连接")
        if success:
            showInfo(message)
        else:
            showWarning(message)

    def _on_fetch_models(self):
        endpoint = self._current_endpoint()
        api_key = self.key_edit.text().strip()
        if not api_key:
            showWarning("请先输入 API Key。")
            return

        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("获取中...")
        self._fetch_worker = _FetchModelsWorker(endpoint, api_key)
        self._fetch_worker.finished.connect(self._on_fetch_result)
        self._fetch_worker.start()

    def _on_fetch_result(self, success, data):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("获取模型")
        if success:
            current = self.model_combo.currentText()
            self.model_combo.clear()
            self.model_combo.addItems(data)
            if current in data:
                self.model_combo.setCurrentText(current)
            showInfo(f"已获取 {len(data)} 个模型。")
        else:
            showWarning(data)

    def _current_endpoint(self):
        """Get the endpoint URL from the current UI state."""
        idx = self.endpoint_preset.currentIndex()
        if idx == self._custom_idx:
            return self.custom_ep_edit.text().strip()
        if idx > 0:
            label = self.endpoint_preset.currentText()
            info = self._preset_data.get(label, {})
            return info.get("endpoint", "")
        return self.config.get("api_endpoint", "")

    def _on_browse_save_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择短文保存目录")
        if path:
            self.save_path_edit.setText(path)


# ═══════════════════════════════════════════════════════════════════════════
# Generation result popup
# ═══════════════════════════════════════════════════════════════════════════

class GenerationResultDialog(QDialog):
    """Small popup shown after passage generation completes."""

    def __init__(self, passages, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生成结果")
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)

        ok = sum(1 for p in passages if p["passage"])
        fail = len(passages) - ok
        total_words = sum(len(p["words"]) for p in passages)

        lines = [
            f"生成篇数：<b>{len(passages)}</b> 篇",
            f"成功：<b style='color:#27ae60;'>{ok}</b> 篇",
        ]
        if fail:
            lines.append(f"失败：<b style='color:#c0392b;'>{fail}</b> 篇")
        lines.append(f"使用单词：<b>{total_words}</b> 个")

        for i, p in enumerate(passages):
            status = "✓" if p["passage"] else "✗"
            color = "#27ae60" if p["passage"] else "#c0392b"
            lines.append(
                f"<span style='color:{color};'>{status} 短文 {i + 1}</span>"
                f" — {len(p['words'])} 词"
            )

        self._label = QLabel("<br>".join(lines))
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        layout.addSpacing(8)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("确定")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)


# ═══════════════════════════════════════════════════════════════════════════
# Save options dialog
# ═══════════════════════════════════════════════════════════════════════════

class SaveOptionsDialog(QDialog):
    """Unified save dialog: TXT, PDF, cloze, MC cloze with deck selection."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._build_ui()
        self.setWindowTitle("保存设置")
        self.setMinimumWidth(440)
        self.resize(460, 420)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── File name prefix ─────────────────────────────────────────────
        prefix_layout = QHBoxLayout()
        prefix_layout.addWidget(QLabel("文件名前缀:"))
        self.prefix_edit = QLineEdit()
        self.prefix_edit.setText(self.config.get("save_prefix", "passage"))
        prefix_layout.addWidget(self.prefix_edit)
        layout.addLayout(prefix_layout)

        # ── Save path ────────────────────────────────────────────────────
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("保存路径:"))
        self.path_edit = QLineEdit()
        self.path_edit.setText(self.config.get("save_path", ""))
        self.path_edit.setPlaceholderText("选择文件夹...")
        path_layout.addWidget(self.path_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._on_browse)
        path_layout.addWidget(browse_btn)
        layout.addLayout(path_layout)

        # ── Format checkboxes ────────────────────────────────────────────
        fmt_group = QGroupBox("保存格式")
        fmt_layout = QVBoxLayout(fmt_group)

        self.save_txt_check = QCheckBox("保存 TXT")
        self.save_txt_check.setChecked(self.config.get("save_txt", True))
        fmt_layout.addWidget(self.save_txt_check)

        self.save_pdf_check = QCheckBox("保存 PDF")
        self.save_pdf_check.setChecked(self.config.get("save_pdf", False))
        fmt_layout.addWidget(self.save_pdf_check)

        self.save_cloze_check = QCheckBox("生成基础挖空卡片 ({{c1::word}})")
        self.save_cloze_check.setChecked(self.config.get("save_cloze", True))
        self.save_cloze_check.toggled.connect(self._update_deck_visibility)
        fmt_layout.addWidget(self.save_cloze_check)

        self.save_mc_check = QCheckBox("生成挖空选择题 (4选项)")
        self.save_mc_check.setChecked(
            self.config.get("save_mc_cloze", True))
        self.save_mc_check.toggled.connect(self._update_deck_visibility)
        fmt_layout.addWidget(self.save_mc_check)

        layout.addWidget(fmt_group)

        # ── Deck selection ───────────────────────────────────────────────
        deck_group = QGroupBox("目标牌组")
        deck_form = QFormLayout(deck_group)

        self.cloze_deck_combo = QComboBox()
        self.cloze_deck_combo.setEditable(True)
        self.cloze_deck_combo.addItem(DEFAULT_DECK_NAME)
        deck_names = _get_deck_names()
        for name in deck_names:
            if self.cloze_deck_combo.findText(name) < 0:
                self.cloze_deck_combo.addItem(name)
        self.cloze_deck_combo.setCurrentText(
            self.config.get("cloze_deck_name", DEFAULT_DECK_NAME))
        self.cloze_deck_label = QLabel("挖空牌组:")
        deck_form.addRow(self.cloze_deck_label, self.cloze_deck_combo)

        self.mc_deck_combo = QComboBox()
        self.mc_deck_combo.setEditable(True)
        self.mc_deck_combo.addItem(DEFAULT_DECK_NAME)
        for name in deck_names:
            if self.mc_deck_combo.findText(name) < 0:
                self.mc_deck_combo.addItem(name)
        self.mc_deck_combo.setCurrentText(
            self.config.get("mc_deck_name", DEFAULT_DECK_NAME))
        self.mc_deck_label = QLabel("选择题牌组:")
        deck_form.addRow(self.mc_deck_label, self.mc_deck_combo)

        layout.addWidget(deck_group)

        self._update_deck_visibility()

        # ── Buttons ──────────────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("保存全部")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        btn_layout.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _on_browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择短文保存目录")
        if path:
            self.path_edit.setText(path)

    def _update_deck_visibility(self):
        show_cloze = self.save_cloze_check.isChecked()
        show_mc = self.save_mc_check.isChecked()
        self.cloze_deck_label.setVisible(show_cloze)
        self.cloze_deck_combo.setVisible(show_cloze)
        self.mc_deck_label.setVisible(show_mc)
        self.mc_deck_combo.setVisible(show_mc)

    def result(self):
        return {
            "save_txt": self.save_txt_check.isChecked(),
            "save_pdf": self.save_pdf_check.isChecked(),
            "save_cloze": self.save_cloze_check.isChecked(),
            "save_mc_cloze": self.save_mc_check.isChecked(),
            "prefix": self.prefix_edit.text().strip() or "passage",
            "save_path": self.path_edit.text().strip(),
            "cloze_deck_name":
                self.cloze_deck_combo.currentText().strip() or DEFAULT_DECK_NAME,
            "mc_deck_name":
                self.mc_deck_combo.currentText().strip() or DEFAULT_DECK_NAME,
            "mc_distractor_sources":
                self.config.get("mc_distractor_sources",
                                ["ai", "today", "collection"]),
            "word_field": self.config.get("word_field", ""),
            "note_type": self.config.get("note_type", ""),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Passage display dialog (tabbed)
# ═══════════════════════════════════════════════════════════════════════════

class PassageDialog(QDialog):
    """Tabbed dialog for displaying / regenerating / saving passages.

    - *passages*: None (loading) or list of {passage, words, error}
    - *word_groups*: list-of-lists of plain strings used for regeneration
    """

    def __init__(self, passages, word_groups, config, parent=None):
        super().__init__(parent)
        self._passages = passages or []
        self._word_groups = word_groups
        self.config = config
        self._tab_edits = []
        self._worker = None
        self._build_ui()
        self.setWindowTitle("今日单词短文")
        self.resize(620, 560)

        if passages is None:
            self._set_loading()
        else:
            self._show_passages(passages)

    # ── build ──────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self.supplement_edit = QTextEdit()
        self.supplement_edit.setPlaceholderText(
            "补充要求（选填，如：难度降低、多用于对话场景...）")
        self.supplement_edit.setMaximumHeight(46)
        layout.addWidget(self.supplement_edit)

        action_layout = QHBoxLayout()

        self.regenerate_btn = QPushButton("换一批")
        self.regenerate_btn.clicked.connect(self._on_regenerate)
        action_layout.addWidget(self.regenerate_btn)

        self.copy_btn = QPushButton("复制当前")
        self.copy_btn.clicked.connect(self._on_copy)
        action_layout.addWidget(self.copy_btn)

        self.save_btn = QPushButton("保存全部")
        self.save_btn.clicked.connect(self._on_save)
        action_layout.addWidget(self.save_btn)

        action_layout.addStretch()
        layout.addLayout(action_layout)

        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.accept)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)

    # ── state helpers ──────────────────────────────────────────────────

    def _set_loading(self):
        self.info_label.setText(
            f"共 <b>{len(self._word_groups)}</b> 篇短文，AI 正在生成，请稍候...")
        self.tabs.clear()
        self._tab_edits = []
        loading_edit = QTextEdit()
        loading_edit.setReadOnly(True)
        loading_edit.setHtml(
            '<p style="color:#888;font-size:16px;text-align:center;'
            'padding:60px;">⏳ AI 正在生成短文...</p>')
        self.tabs.addTab(loading_edit, "生成中")
        self._set_buttons_enabled(False)
        self.close_btn.setEnabled(True)

    def _set_buttons_enabled(self, enabled):
        self.regenerate_btn.setEnabled(enabled)
        self.copy_btn.setEnabled(enabled)
        self.save_btn.setEnabled(enabled)

    def _show_passages(self, passages):
        self._passages = passages
        self.tabs.clear()
        self._tab_edits = []
        self.supplement_edit.setEnabled(True)

        total_words = sum(len(p["words"]) for p in passages)
        ok = sum(1 for p in passages if p["passage"])
        fail = len(passages) - ok
        parts = [f"共 <b>{len(passages)}</b> 篇，{total_words} 个单词"]
        if fail:
            parts.append(
                f"<span style='color:#c0392b;'>{fail} 篇失败</span>")
        self.info_label.setText("，".join(parts))

        for i, p in enumerate(passages):
            edit = QTextEdit()
            edit.setReadOnly(True)
            if p["passage"]:
                edit.setHtml(self._highlight_words(p["passage"], p["words"]))
            else:
                edit.setHtml(
                    f'<p style="color:#c0392b;padding:20px;">'
                    f'生成失败：{p.get("error", "未知错误")}</p>')
            self._tab_edits.append(edit)
            self.tabs.addTab(edit, f"短文 {i + 1}")

        self._set_buttons_enabled(ok > 0)

    # ── public API ─────────────────────────────────────────────────────

    def set_passages(self, passages):
        self._passages = passages
        self._show_passages(passages)

    def show_error(self, error):
        self.info_label.setText("生成失败")
        self.tabs.clear()
        self._tab_edits = []
        err_edit = QTextEdit()
        err_edit.setReadOnly(True)
        err_edit.setHtml(
            f'<p style="color:#c0392b;padding:20px;">{error}</p>')
        self.tabs.addTab(err_edit, "错误")
        self._set_buttons_enabled(False)
        self.regenerate_btn.setEnabled(True)

    # ── worker callback (connected as slot — lambda-free) ──────────────

    def _on_worker_done(self, results):
        """Slot called by _GenerateWorker.finished in the main thread."""
        passages = []
        for r in results:
            passages.append({
                "passage": r["passage"],
                "words": r["words"],
                "error": r["error"],
            })

        if any(p["passage"] for p in passages):
            self.set_passages(passages)
            # Show result summary popup
            popup = GenerationResultDialog(passages, parent=self)
            popup.show()
        else:
            first_err = next(
                (p["error"] for p in passages if p["error"]), "未知错误")
            self.show_error(first_err)

    # ── highlight ──────────────────────────────────────────────────────

    def _highlight_words(self, text, words):
        from .generator import parse_form_changes
        form_changes = parse_form_changes(text)
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        all_words = list(words) + [v for v in form_changes.values() if v not in words]
        for word in sorted(all_words, key=len, reverse=True):
            pattern = re.compile(
                r'\b(' + re.escape(word) + r')\b', re.IGNORECASE)
            escaped = pattern.sub(
                r'<b style="color:#c0392b;">\1</b>', escaped)
        return escaped.replace("\n", "<br>")

    # ── actions ────────────────────────────────────────────────────────

    def _on_copy(self):
        idx = self.tabs.currentIndex()
        if 0 <= idx < len(self._passages) and self._passages[idx]["passage"]:
            QApplication.clipboard().setText(
                self._passages[idx]["passage"])
            tooltip(f"短文 {idx + 1} 已复制到剪贴板。")
        else:
            tooltip("当前页无内容可复制。")

    def _on_regenerate(self):
        supplement = self.supplement_edit.toPlainText().strip()
        self._set_loading()
        self.supplement_edit.setEnabled(False)

        # Pre-extract plain data (same as initial generation)
        api_params = {
            "api_endpoint": self.config["api_endpoint"],
            "api_key": self.config["api_key"],
            "model": self.config["model"],
            "temperature": self.config["temperature"],
            "max_tokens": self.config["max_tokens"],
        }
        prompt_params = {
            "base_prompt": self.config.get("system_prompt", ""),
            "output_words": self.config.get("output_words", 200),
            "classify_vocab": self.config.get("classify_vocab", True),
            "include_translation": self.config.get("include_translation", False),
        }

        self._worker = _GenerateWorker(
            self._word_groups, api_params, prompt_params, supplement)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    def _on_save(self):
        if not self._passages or all(
                p.get("passage") is None for p in self._passages):
            showWarning("没有可保存的短文。")
            return

        dlg = SaveOptionsDialog(self.config, parent=self)
        if dlg.exec() == 0:
            return

        options = dlg.result()
        result = save_all_passages(self._passages, options)

        # Build summary
        parts = []
        if (options.get("save_txt") or options.get("save_pdf")) and not options.get("save_path"):
            showWarning("未设置保存路径，文件将被跳过。\n请在设置中配置保存路径后重试。")
        if options.get("save_txt") and result["txt"]:
            parts.append(f"TXT: {len(result['txt'])} 篇")
        if options.get("save_pdf") and result["pdf"]:
            parts.append(f"PDF: {len(result['pdf'])} 篇")
        if options.get("save_cloze"):
            a, s, e = result["cloze"]
            if a > 0:
                parts.append(f"挖空卡片: {a} 张")
            if e:
                showWarning("\n".join(e))
        if options.get("save_mc_cloze"):
            a, s, e = result["mc_cloze"]
            if a > 0:
                parts.append(f"选择题: {a} 张")
            if e:
                showWarning("\n".join(e))

        if parts:
            tooltip("；".join(parts))
        elif (not options.get("save_txt")
              and not options.get("save_pdf")
              and not options.get("save_cloze")
              and not options.get("save_mc_cloze")):
            showWarning("未选择任何保存方式。")
        else:
            showWarning("保存失败，请检查保存路径设置。")
