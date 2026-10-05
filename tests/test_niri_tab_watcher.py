import unittest

from niri_tab_watcher import FocusTracker


def window(window_id, column, *, focused=False, width=50, row=1, workspace=1):
    return {
        "id": window_id,
        "title": str(window_id),
        "app_id": "test",
        "workspace_id": workspace,
        "is_focused": focused,
        "is_floating": False,
        "layout": {
            "pos_in_scrolling_layout": [column, row],
            "tile_size": [width, 100],
            "window_size": [width, 100],
            "tile_pos_in_workspace_view": None,
            "window_offset_in_tile": [0, 0],
        },
    }


class FocusTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = FocusTracker(debounce_seconds=0.75, expanded_width_ratio=0.9)
        self.tracker.set_outputs({"DP-1": {"logical": {"width": 100}}})
        self.tracker.apply_event(
            {
                "WorkspacesChanged": {
                    "workspaces": [{"id": 1, "output": "DP-1", "is_focused": True}]
                }
            },
            0,
        )

    def initialize(self, windows):
        self.tracker.apply_event({"WindowsChanged": {"windows": windows}}, 0)

    def focus(self, window_id, now):
        return self.tracker.apply_event({"WindowFocusChanged": {"id": window_id}}, now)

    def test_focus_transition_commits_only_after_debounce(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])

        self.focus(2, 1)
        self.assertEqual(self.tracker.stable_id, 1)
        self.assertIsNone(self.tracker.previous)
        self.assertFalse(self.tracker.commit_due(1.74))

        self.assertTrue(self.tracker.commit_due(1.75))
        self.assertEqual(self.tracker.stable_id, 2)
        self.assertEqual(self.tracker.previous.id, 1)

    def test_short_accidental_focus_is_not_recorded(self):
        self.initialize([window(1, 1, focused=True), window(2, 2), window(3, 3)])

        self.focus(2, 1)
        self.focus(3, 1.5)
        self.assertFalse(self.tracker.commit_due(2.24))
        self.assertTrue(self.tracker.commit_due(2.25))

        self.assertEqual(self.tracker.stable_id, 3)
        self.assertEqual(self.tracker.previous.id, 1)

    def test_returning_during_debounce_discards_candidate(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])

        self.focus(2, 1)
        self.focus(1, 1.5)

        self.assertIsNone(self.tracker.pending)
        self.assertEqual(self.tracker.stable_id, 1)
        self.assertIsNone(self.tracker.previous)

    def test_temporary_non_window_focus_does_not_enter_history(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])

        self.focus(None, 1)
        self.focus(1, 2)

        self.assertEqual(self.tracker.stable_id, 1)
        self.assertIsNone(self.tracker.pending)
        self.assertIsNone(self.tracker.previous)

    def test_window_after_non_window_focus_uses_stable_departure(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])

        self.focus(None, 1)
        self.focus(2, 2)
        self.tracker.commit_due(2.75)

        self.assertEqual(self.tracker.stable_id, 2)
        self.assertEqual(self.tracker.previous.id, 1)

    def test_restore_during_debounce_returns_to_stable_window(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])
        self.focus(2, 1)

        plan = self.tracker.begin_restore()

        self.assertEqual(plan.target_id, 1)
        self.assertEqual(plan.focus_ids, (1,))
        self.focus(1, 1.1)
        self.assertEqual(self.tracker.stable_id, 1)
        self.assertIsNone(self.tracker.previous)
        self.assertIsNone(self.tracker.pending)

    def test_right_anchor_restores_b_plus_c_without_polluting_history(self):
        a = window(1, 1)
        b = window(2, 2)
        c = window(3, 3, focused=True)
        self.initialize([a, b, c])

        self.focus(2, 1)
        self.tracker.commit_due(1.75)
        self.focus(1, 2)
        self.tracker.commit_due(2.75)

        plan = self.tracker.begin_restore()
        self.assertEqual(plan.target_id, 2)
        self.assertEqual(plan.focus_ids, (3, 2))

        self.focus(3, 3)
        self.focus(2, 3.1)

        self.assertEqual(self.tracker.stable_id, 2)
        self.assertEqual(self.tracker.previous.id, 1)
        self.assertIsNone(self.tracker.restore_transaction)

    def test_cross_workspace_restore_directly_focuses_left_oriented_target(self):
        a = window(1, 1, focused=True, workspace=2)
        b = window(2, 2, workspace=2)
        c = window(3, 3, workspace=2)
        other_workspace_window = window(4, 1, workspace=1)
        self.initialize([a, b, c, other_workspace_window])

        # Entering B from A establishes the visible A+B orientation.
        self.focus(2, 1)
        self.tracker.commit_due(1.75)
        self.focus(4, 2)
        self.tracker.commit_due(2.75)

        plan = self.tracker.begin_restore()

        self.assertEqual(plan.target_id, 2)
        self.assertEqual(plan.focus_ids, (2,))

    def test_expanded_target_skips_anchor(self):
        a = window(1, 1)
        b = window(2, 2, width=95)
        c = window(3, 3, focused=True)
        self.initialize([a, b, c])

        self.focus(2, 1)
        self.tracker.commit_due(1.75)
        self.focus(1, 2)
        self.tracker.commit_due(2.75)

        plan = self.tracker.begin_restore()
        self.assertEqual(plan.focus_ids, (2,))

    def test_closed_previous_window_is_invalidated(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])
        self.focus(2, 1)
        self.tracker.commit_due(1.75)

        self.tracker.apply_event({"WindowClosed": {"id": 1}}, 2)

        self.assertIsNone(self.tracker.previous)
        self.assertIsNone(self.tracker.begin_restore())

    def test_closing_stable_window_promotes_already_focused_window(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])
        self.focus(2, 1)

        self.tracker.apply_event({"WindowClosed": {"id": 1}}, 1.1)

        self.assertEqual(self.tracker.stable_id, 2)
        self.assertEqual(self.tracker.observed_id, 2)
        self.assertIsNone(self.tracker.pending)

    def test_closing_stable_window_before_focus_event_resets_baseline(self):
        self.initialize([window(1, 1, focused=True), window(2, 2)])

        self.tracker.apply_event({"WindowClosed": {"id": 1}}, 1)
        self.focus(2, 1.1)

        self.assertEqual(self.tracker.stable_id, 2)
        self.assertIsNone(self.tracker.previous)

    def test_closing_pending_candidate_preserves_departed_snapshot(self):
        self.initialize([window(1, 1, focused=True), window(2, 2), window(3, 3)])
        self.focus(2, 1)
        self.tracker.apply_event({"WindowClosed": {"id": 2}}, 1.1)
        self.focus(3, 1.2)
        self.tracker.commit_due(1.95)

        self.assertEqual(self.tracker.stable_id, 3)
        self.assertEqual(self.tracker.previous.id, 1)

    def test_closing_restore_anchor_cancels_transaction(self):
        a = window(1, 1)
        b = window(2, 2)
        c = window(3, 3, focused=True)
        self.initialize([a, b, c])
        self.focus(2, 1)
        self.tracker.commit_due(1.75)
        self.focus(1, 2)
        self.tracker.commit_due(2.75)
        self.assertEqual(self.tracker.begin_restore().focus_ids, (3, 2))

        self.tracker.apply_event({"WindowClosed": {"id": 3}}, 3)

        self.assertIsNone(self.tracker.restore_transaction)
        self.assertEqual(self.tracker.stable_id, 1)

    def test_unexpected_focus_cancels_restore_and_starts_debounce(self):
        a = window(1, 1)
        b = window(2, 2)
        c = window(3, 3, focused=True)
        d = window(4, 4)
        self.initialize([a, b, c, d])
        self.focus(2, 1)
        self.tracker.commit_due(1.75)
        self.focus(1, 2)
        self.tracker.commit_due(2.75)
        self.tracker.begin_restore()

        self.focus(4, 3)

        self.assertIsNone(self.tracker.restore_transaction)
        self.assertEqual(self.tracker.observed_id, 4)
        self.assertEqual(self.tracker.pending.candidate_id, 4)

    def test_layout_updates_are_used_for_anchor_selection(self):
        a = window(1, 1)
        b = window(2, 2)
        c = window(3, 3, focused=True)
        self.initialize([a, b, c])
        self.focus(2, 1)
        self.tracker.commit_due(1.75)
        self.focus(1, 2)
        self.tracker.commit_due(2.75)

        moved_layout = dict(c["layout"])
        moved_layout["pos_in_scrolling_layout"] = [4, 1]
        self.tracker.apply_event(
            {"WindowLayoutsChanged": {"changes": [[3, moved_layout]]}}, 3
        )

        self.assertEqual(self.tracker.begin_restore().focus_ids, (3, 2))


if __name__ == "__main__":
    unittest.main()
