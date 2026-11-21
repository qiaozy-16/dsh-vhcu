"""P6 virtual-cursor coordinate mapping and message-shape tests."""

from __future__ import annotations

import unittest
from unittest import mock

from vhcu_daemon.virtual_cursor import (
    MK_LBUTTON,
    MK_MBUTTON,
    MK_RBUTTON,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    VirtualCursorUnsupported,
    is_postmessage_unsupported_class,
    is_shell_window_class,
    is_system_process_name,
    map_client_coordinates,
    mouse_messages,
    path_messages,
    wheel_message,
)


class DpiMappingTest(unittest.TestCase):
    def test_per_monitor_keeps_physical(self):
        self.assertEqual(map_client_coordinates(200, 100, 144, "per-monitor"), (200, 100))
        self.assertEqual(map_client_coordinates(200, 100, 192, "per-monitor-v2"), (200, 100))

    def test_system_aware_scales_to_logical(self):
        self.assertEqual(map_client_coordinates(200, 100, 144, "system"), (133, 67))
        self.assertEqual(map_client_coordinates(200, 100, 120, "unaware"), (160, 80))
        self.assertEqual(map_client_coordinates(97, 49, 96, "system"), (97, 49))

    def test_unknown_awareness_is_unsupported(self):
        with self.assertRaises(VirtualCursorUnsupported):
            map_client_coordinates(1, 1, 96, "weird")


class ShellWindowTest(unittest.TestCase):
    def test_taskbar_and_desktop_are_unsupported(self):
        self.assertTrue(is_shell_window_class("Shell_TrayWnd"))
        self.assertTrue(is_shell_window_class("Shell_SecondaryTrayWnd"))
        self.assertTrue(is_shell_window_class("Progman"))
        self.assertTrue(is_shell_window_class("WorkerW"))
        self.assertTrue(is_shell_window_class("#32770"))
        self.assertFalse(is_shell_window_class("Notepad"))
        self.assertFalse(is_shell_window_class(""))

    def test_explorer_and_shell_processes_are_unsupported(self):
        self.assertTrue(is_system_process_name("explorer.exe"))
        self.assertTrue(is_system_process_name("EXPLORER.EXE"))
        self.assertTrue(is_system_process_name("StartMenuExperienceHost.exe"))
        self.assertFalse(is_system_process_name("notepad.exe"))
        self.assertFalse(is_system_process_name(""))

    def test_cef_and_uwp_classes_are_unsupported_but_chromium_is_allowed(self):
        # V1.1: real Chromium child surfaces accept the posted click, so the
        # Chrome_WidgetWin_* main-window classes were removed from the blocklist.
        self.assertFalse(is_postmessage_unsupported_class("Chrome_WidgetWin_1"))
        self.assertFalse(is_postmessage_unsupported_class("Chrome_WidgetWin_0"))
        self.assertTrue(is_postmessage_unsupported_class("CefBrowserWindow"))
        self.assertTrue(is_postmessage_unsupported_class("ApplicationFrameWindow"))
        self.assertTrue(is_postmessage_unsupported_class("Windows.UI.Core.CoreWindow"))
        self.assertFalse(is_postmessage_unsupported_class("Notepad"))
        self.assertFalse(is_postmessage_unsupported_class(""))

    def test_reject_shell_window_blocks_uwp_before_posting(self):
        from vhcu_daemon import virtual_cursor
        with mock.patch.object(virtual_cursor, "window_class_name", return_value="ApplicationFrameWindow"):
            with self.assertRaises(VirtualCursorUnsupported) as raised:
                virtual_cursor._reject_shell_window(1)
        self.assertEqual(raised.exception.code, "E_VIRTUAL_CURSOR_UNSUPPORTED")


class PathAndWheelTest(unittest.TestCase):
    def test_every_drag_move_carries_mk_lbutton(self):
        sequence = path_messages([(10, 10), (20, 20), (30, 30)])
        self.assertEqual(sequence[0], (WM_MOUSEMOVE, 0, 0x000A000A))
        self.assertEqual(sequence[1], (WM_LBUTTONDOWN, MK_LBUTTON, 0x000A000A))
        for message, wparam, _lparam in sequence[2:-1]:
            self.assertEqual(message, WM_MOUSEMOVE)
            self.assertEqual(wparam, MK_LBUTTON)
        self.assertEqual(sequence[-1][0], WM_LBUTTONUP)

    def test_wheel_message_uses_wheel_delta_and_screen_coords(self):
        message, wparam, lparam = wheel_message(2, 100, 200)
        self.assertEqual(message, WM_MOUSEWHEEL)
        self.assertEqual((wparam >> 16) & 0xFFFF, 240)
        self.assertEqual(lparam & 0xFFFF, 100)
        self.assertEqual((lparam >> 16) & 0xFFFF, 200)


class MessageShapeTest(unittest.TestCase):
    def test_click_sequence_and_button_masks(self):
        seq = mouse_messages("click", 100, 200)
        self.assertEqual(seq, [
            (WM_MOUSEMOVE, 0),
            (WM_LBUTTONDOWN, MK_LBUTTON),
            (WM_LBUTTONUP, 0),
        ])

    def test_double_right_middle(self):
        double = mouse_messages("double_click", 10, 20)
        self.assertEqual(double, [
            (WM_MOUSEMOVE, 0),
            (WM_LBUTTONDOWN, MK_LBUTTON),
            (WM_LBUTTONUP, 0),
            (WM_LBUTTONDOWN, MK_LBUTTON),
            (WM_LBUTTONUP, 0),
        ])
        self.assertEqual(double.count((WM_LBUTTONUP, 0)), 2)
        self.assertEqual(mouse_messages("right_click", 1, 2), [
            (WM_MOUSEMOVE, 0),
            (WM_RBUTTONDOWN, MK_RBUTTON),
            (WM_RBUTTONUP, 0),
        ])
        self.assertEqual(mouse_messages("middle_click", 1, 2), [
            (WM_MOUSEMOVE, 0),
            (WM_MBUTTONDOWN, MK_MBUTTON),
            (WM_MBUTTONUP, 0),
        ])

    def test_unknown_action(self):
        with self.assertRaises(VirtualCursorUnsupported):
            mouse_messages("scroll", 1, 2)


if __name__ == "__main__":
    unittest.main()
