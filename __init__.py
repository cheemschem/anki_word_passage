"""AI Word Passage Generator - Anki add-on entry point.

Generates short passages from today's due words using an AI API
to help with word memorization.
"""

from aqt import mw
from aqt.qt import QAction
from aqt.gui_hooks import (
    deck_browser_will_render_content,
    state_did_change,
    webview_did_receive_js_message,
)
from .config import ADDON_NAME, get_config
from .generator import generate_and_show, auto_generate_if_enabled
from .cloze_mc import reset_collection_cache
from .ui import SettingsDialog


def _on_generate():
    """Menu / button action: generate passage for today's words."""
    generate_and_show(mw)


def _on_settings():
    """Open the settings dialog."""
    dialog = SettingsDialog(mw)
    dialog.exec()


def _inject_deck_browser_button(deck_browser, content):
    """Inject a generate button into the deck browser overview page."""
    content.tree += """
<div style="text-align:center; margin:12px 0;">
    <button onclick="pycmd('word_passage_generate')"
            style="padding:8px 16px; font-size:14px; cursor:pointer;
                   background:#4a90d9; color:white; border:none; border-radius:4px;">
        生成今日单词短文
    </button>
</div>
"""


def _handle_js_message(handled, message, context):
    """Handle pycmd commands from injected HTML buttons."""
    if message == "word_passage_generate":
        _on_generate()
        return (True, None)
    return handled


# Track auto-generate to fire only once per session
_auto_fired = False


def _on_state_change(new_state, old_state):
    """Auto-generate passage when entering review state, if configured."""
    global _auto_fired
    if new_state == "review" and not _auto_fired:
        if get_config().get("auto_generate"):
            _auto_fired = True
            auto_generate_if_enabled()


def _reset_auto_flag():
    global _auto_fired
    _auto_fired = False
    reset_collection_cache()


# --- Register hooks and menu items ---

if mw is not None:
    # Menu: Tools → Generate passage
    generate_action = QAction("生成今日单词短文", mw)
    generate_action.triggered.connect(_on_generate)
    mw.form.menuTools.addAction(generate_action)

    # Menu: Tools → Settings
    settings_action = QAction("AI 单词短文设置...", mw)
    settings_action.triggered.connect(_on_settings)
    mw.form.menuTools.addAction(settings_action)

    # Add-on manager settings button
    mw.addonManager.setConfigAction(ADDON_NAME, _on_settings)

    # Deck browser: inject generate button
    deck_browser_will_render_content.append(_inject_deck_browser_button)

    # Handle injected button clicks
    webview_did_receive_js_message.append(_handle_js_message)

    # Auto-generate when entering review
    state_did_change.append(_on_state_change)

    # Reset on profile close
    from aqt.gui_hooks import profile_will_close
    profile_will_close.append(_reset_auto_flag)
