"""Tests for the safety policy module."""

import unittest

from qwen_omarchy_control import policy


class PolicyLevelTest(unittest.TestCase):
    def test_level1_queries(self):
        for op in ("get_active_window", "list_windows", "list_workspaces",
                   "get_monitors", "switch_workspace", "focus_window",
                   "launch_app", "set_volume", "volume_up", "volume_down",
                   "mute_audio", "unmute_audio", "get_audio_status",
                   "get_system_status"):
            self.assertEqual(policy.classify(op), "level1", op)

    def test_level2_careful(self):
        for op in ("move_active_window_to_workspace", "close_active_window",
                   "open_url", "type_text"):
            self.assertEqual(policy.classify(op), "level2", op)

    def test_level3_denied(self):
        for op in ("run_shell", "delete_file", "overwrite_file", "shutdown",
                   "reboot", "kill_process", "send_message", "submit_form",
                   "install_package", "remove_package"):
            with self.assertRaises(policy.Denied, msg=op):
                policy.classify(op)

    def test_unknown_fails_closed(self):
        with self.assertRaises(policy.PolicyError):
            policy.classify("sudo everything")

    def test_max_level(self):
        self.assertEqual(policy.max_level(["get_active_window"]), "level1")
        self.assertEqual(policy.max_level(["launch_app", "open_url"]), "level2")
        with self.assertRaises(policy.PolicyError):
            policy.max_level(["nonsense"])

    def test_sensitive_text_guard(self):
        self.assertTrue(policy.reject_sensitive_text("type my password into the entry"))
        self.assertTrue(policy.reject_sensitive_text("ssn 123-45-6789"))
        self.assertTrue(policy.reject_sensitive_text("credit card 4111 xxxx"))
        self.assertFalse(policy.reject_sensitive_text("type hello world"))


if __name__ == "__main__":
    unittest.main()