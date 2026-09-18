"""Tests for the patch-free microphone-state classifier.

The classifier reads the stock TUI's visible output. Its failure modes are the
interesting part: `unknown` must never be treated as "muted", because that once
let a fresh start report a confirmed mute while the microphone was open.
"""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_TOOL = REPO / "bin" / "qwen-voice-state"


def load_state_tool():
    loader = importlib.machinery.SourceFileLoader("qwen_voice_state", str(STATE_TOOL))
    spec = importlib.util.spec_from_loader("qwen_voice_state", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


tool = None


def setUpModule():
    global tool
    tool = load_state_tool()


class ClassifyTest(unittest.TestCase):
    def test_session_gone_is_stopped(self):
        self.assertEqual(tool.classify_exact(None), "stopped")
        self.assertEqual(tool.classify(None), ("stopped", "Voice assistant stopped"))

    def test_empty_pane_is_unknown_not_muted(self):
        self.assertEqual(tool.classify_exact(""), "unknown")
        # Published as muted (dimmed) so the bar fails safe, but the raw state
        # stays distinguishable for the toggle script.
        status, _ = tool.classify("")
        self.assertEqual(status, "muted")

    def test_latest_transition_wins(self):
        pane = (
            "[麦克风已开启 · 16000 Hz · PortAudio 半双工]\n"
            "[麦克风已静音，语音输入不会被识别；输入 /mute 恢复]\n"
        )
        self.assertEqual(tool.classify_exact(pane), "muted")

    def test_listening_after_muted(self):
        pane = (
            "[麦克风已静音，语音输入不会被识别；输入 /mute 恢复]\n"
            "[麦克风已恢复]\n"
            "[麦克风已开启 · 16000 Hz · PortAudio 半双工]\n"
        )
        self.assertEqual(tool.classify_exact(pane), "listening")

    def test_self_mute_on_other_client(self):
        # The TUI mutes itself when another client takes the voice and does not
        # update the persistent status line.
        pane = "已连接 · 麦克风已开启 · PortAudio 半双工\n[语音正由另一窗口使用]\n"
        self.assertEqual(tool.classify_exact(pane), "muted")

    def test_voice_switched_away(self):
        pane = "[语音已切换到另一窗口]\n"
        self.assertEqual(tool.classify_exact(pane), "muted")

    def test_help_text_alone_is_not_a_state(self):
        # The help block mentions /mute; it must not be read as a transition.
        pane = "  /mute           静音 / 恢复麦克风\n"
        self.assertEqual(tool.classify_exact(pane), "unknown")


if __name__ == "__main__":
    unittest.main()
