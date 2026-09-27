import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    import fcntl
except ImportError:
    fcntl = None


MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "session-manager.py"
SPEC = importlib.util.spec_from_file_location("session_manager", MODULE_PATH)
session_manager = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(session_manager)


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        # Keep decision tests hermetic: no real pgrep/lock probing, and the
        # runtime rollback switch is treated as absent.
        for name in ("has_active_background_task", "idle_use_last_record"):
            patcher = mock.patch.object(session_manager, name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _soft_pressure_fixture(self, now, **metric_overrides):
        metrics = {
            "session_id": "44444444-4444-4444-4444-444444444444",
            "pressure_ratio": 0.55,
            "user_turns": 20,
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "last_user_at": (now - timedelta(minutes=11)).isoformat(),
        }
        metrics.update(metric_overrides)
        state = {
            "last_activity": metrics["last_user_at"],
            "last_reset": (now - timedelta(hours=2)).isoformat(),
            "last_daily_reset_date": now.date().isoformat(),
        }
        return metrics, state

    def test_failed_daily_compact_does_not_retry_same_day(self):
        now = datetime(2026, 9, 13, 12, 0, 0)
        metrics, state = self._soft_pressure_fixture(now, pressure_ratio=0.2,
            last_user_at=(now - timedelta(hours=3)).isoformat())
        state.update(last_activity=metrics['last_user_at'], last_daily_reset_date='2026-09-12',
                     last_daily_compact_attempt_date=now.date().isoformat())
        with mock.patch.object(session_manager, 'compact_mode_enabled', return_value=True):
            decision = session_manager.evaluate_reset(state, metrics, now)
            self.assertIsNone(decision['reason'])
            metrics['pressure_ratio'] = 0.75
            decision = session_manager.evaluate_reset(state, metrics, now)
            self.assertEqual(decision['reason'], 'context_pressure_hard')

    def test_active_session_id_uses_latest_log_entry(self):
        old = "11111111-1111-1111-1111-111111111111"
        new = "22222222-2222-2222-2222-222222222222"
        text = f"[acp] session created: {old}\n[acp] prompt: hi (session={new})\n"
        self.assertEqual(session_manager.active_session_id_from_text(text), new)

    def test_old_session_rotates_even_when_recently_compacted(self):
        now = datetime(2026, 9, 13, 12)
        metrics, state = self._soft_pressure_fixture(now, pressure_ratio=0.1,
            started_at=(now-timedelta(hours=25)).isoformat(),
            last_user_at=(now-timedelta(minutes=40)).isoformat(),
            last_compact_at=(now-timedelta(minutes=5)).isoformat(),
            context_tokens=2000, last_compact_post_tokens=2000)
        state['last_activity'] = metrics['last_user_at']
        with mock.patch.object(session_manager, 'compact_mode_enabled', return_value=True):
            d = session_manager.evaluate_reset(state, metrics, now)
            self.assertEqual((d['reason'], d['action']), ('age_reset','rotate'))
            state['last_rotated_session_id'] = metrics['session_id']
            self.assertIsNone(session_manager.evaluate_reset(state, metrics, now)['reason'])

    def test_daily_maintenance_creates_new_session_in_compact_mode(self):
        now = datetime(2026, 9, 13, 5)
        metrics, state = self._soft_pressure_fixture(now, pressure_ratio=0.1,
            last_user_at=(now-timedelta(hours=3)).isoformat())
        state.update(last_activity=metrics['last_user_at'], last_daily_reset_date='2026-09-12')
        with mock.patch.object(session_manager, 'compact_mode_enabled', return_value=True):
            d=session_manager.evaluate_reset(state, metrics, now)
            self.assertEqual((d['reason'],d['action']),('daily_reset','rotate'))

    def test_pressure_still_compacts_and_active_work_defers_age_rotation(self):
        now = datetime(2026, 9, 13, 12)
        metrics,state=self._soft_pressure_fixture(now)
        with mock.patch.object(session_manager, 'compact_mode_enabled', return_value=True):
            self.assertEqual(session_manager.evaluate_reset(state,metrics,now)['action'],'compact')
            metrics.update(pressure_ratio=0.1,started_at=(now-timedelta(hours=25)).isoformat(),
                last_work_at=(now-timedelta(minutes=2)).isoformat())
            self.assertIsNone(session_manager.evaluate_reset(state,metrics,now)['reason'])

    def test_metrics_use_real_usage_and_count_only_enqueued_turns(self):
        session_id = "33333333-3333-3333-3333-333333333333"
        records = [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": "2026-07-11T01:00:00Z",
            },
            {
                "type": "user",
                "timestamp": "2026-07-11T01:00:01Z",
                "message": {"role": "user", "content": [{"type": "tool_result"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-11T01:00:02Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 80_000,
                        "cache_creation_input_tokens": 1_000,
                    },
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            metrics = session_manager.read_session_metrics(session_id, transcript)
        self.assertEqual(metrics["user_turns"], 1)
        self.assertEqual(metrics["context_tokens"], 81_010)
        self.assertEqual(metrics["model"], "claude-test")
        self.assertAlmostEqual(metrics["pressure_ratio"], 0.4051)

    def test_soft_pressure_rotates_only_after_idle_window(self):
        now = datetime(2026, 7, 11, 12, 0, 0)
        metrics = {
            "session_id": "44444444-4444-4444-4444-444444444444",
            "pressure_ratio": 0.55,
            "user_turns": 20,
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "last_user_at": (now - timedelta(minutes=11)).isoformat(),
        }
        state = {
            "last_activity": metrics["last_user_at"],
            "last_reset": (now - timedelta(hours=2)).isoformat(),
            "last_daily_reset_date": now.date().isoformat(),
        }
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertEqual(decision["reason"], "context_pressure")

        metrics["last_user_at"] = (now - timedelta(minutes=5)).isoformat()
        state["last_activity"] = metrics["last_user_at"]
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertIsNone(decision["reason"])

    def test_hard_pressure_and_repeat_guard(self):
        now = datetime(2026, 7, 11, 12, 0, 0)
        session_id = "55555555-5555-5555-5555-555555555555"
        metrics = {
            "session_id": session_id,
            "pressure_ratio": 0.72,
            "user_turns": 30,
            "started_at": (now - timedelta(hours=3)).isoformat(),
            "last_user_at": (now - timedelta(minutes=4)).isoformat(),
        }
        state = {
            "last_activity": metrics["last_user_at"],
            "last_reset": (now - timedelta(hours=3)).isoformat(),
            "last_daily_reset_date": now.date().isoformat(),
        }
        self.assertEqual(
            session_manager.evaluate_reset(state, metrics, now)["reason"],
            "context_pressure_hard",
        )
        state["last_rotated_session_id"] = session_id
        self.assertIsNone(session_manager.evaluate_reset(state, metrics, now)["reason"])

    def test_recent_assistant_work_defers_soft_rotation(self):
        now = datetime(2026, 7, 11, 12, 0, 0)
        metrics, state = self._soft_pressure_fixture(
            now, last_work_at=(now - timedelta(minutes=5)).isoformat()
        )
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertIsNone(decision["reason"])
        self.assertEqual(decision["idle_from"], "work_record")

    def test_system_records_do_not_touch_activity_fields(self):
        records = [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": "2026-07-11T01:00:00Z",
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-11T01:05:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 10},
                },
            },
            {"type": "system", "timestamp": "2026-07-11T01:20:00Z"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            metrics = session_manager.read_session_metrics("66", transcript)
        self.assertEqual(metrics["last_user_at"], "2026-07-11T01:00:00Z")
        self.assertEqual(metrics["last_work_at"], "2026-07-11T01:05:00Z")
        self.assertEqual(metrics["last_record_at"], "2026-07-11T01:20:00Z")

    def test_fresh_last_record_at_does_not_delay_soft_rotation(self):
        now = datetime(2026, 7, 11, 12, 0, 0)
        metrics, state = self._soft_pressure_fixture(
            now,
            last_work_at="",
            last_record_at=(now - timedelta(minutes=1)).isoformat(),
        )
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertEqual(decision["reason"], "context_pressure")
        self.assertEqual(decision["idle_from"], "user")

    def test_bgtask_hold_defers_soft_rotation_only(self):
        now = datetime(2026, 7, 11, 12, 0, 0)
        metrics, state = self._soft_pressure_fixture(now)
        with mock.patch.object(
            session_manager, "has_active_background_task", return_value=True
        ):
            decision = session_manager.evaluate_reset(state, metrics, now)
            self.assertIsNone(decision["reason"])
            self.assertEqual(decision["idle_from"], "bgtask_hold")

            metrics["pressure_ratio"] = 0.72
            decision = session_manager.evaluate_reset(state, metrics, now)
            self.assertEqual(decision["reason"], "context_pressure_hard")

    def test_compact_boundary_resets_pressure_counters(self):
        records = [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": "2026-08-31T01:00:00Z",
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-31T01:00:05Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 81_000},
                },
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": "2026-08-31T02:00:00Z",
                "compactMetadata": {"preTokens": 81_000, "postTokens": 2_000},
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": "2026-08-31T03:00:00Z",
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-31T03:00:05Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 25_000},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            metrics = session_manager.read_session_metrics("77", transcript)
        self.assertEqual(metrics["compact_count"], 1)
        self.assertEqual(metrics["last_compact_at"], "2026-08-31T02:00:00Z")
        self.assertEqual(metrics["last_compact_post_tokens"], 2_000)
        # Pressure counts only what accrued after the boundary.
        self.assertEqual(metrics["user_turns"], 1)
        self.assertEqual(metrics["total_user_turns"], 2)
        self.assertEqual(metrics["context_tokens"], 25_000)

    def test_compact_boundary_post_tokens_stand_until_next_usage(self):
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-08-31T01:00:05Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "usage": {"input_tokens": 81_000},
                },
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": "2026-08-31T02:00:00Z",
                "compactMetadata": {"preTokens": 81_000, "postTokens": 2_000},
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n")
            metrics = session_manager.read_session_metrics("88", transcript)
        self.assertEqual(metrics["context_tokens"], 2_000)

    def test_fresh_compact_blocks_pressure_until_growth(self):
        now = datetime(2026, 8, 31, 12, 0, 0)
        metrics, state = self._soft_pressure_fixture(
            now,
            context_tokens=110_000,
            last_compact_at=(now - timedelta(minutes=30)).isoformat(),
            last_compact_post_tokens=100_000,
        )
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertIsNone(decision["reason"])
        self.assertFalse(decision["compact_allowed"])

        metrics["last_compact_post_tokens"] = 50_000
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertEqual(decision["reason"], "context_pressure")
        self.assertTrue(decision["compact_allowed"])

        # Growth alone is not enough right after a compact; the interval
        # gate must pass too.
        metrics["last_compact_at"] = (now - timedelta(minutes=5)).isoformat()
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertIsNone(decision["reason"])

    def test_age_reset_fires_in_both_modes(self):
        now = datetime(2026, 8, 31, 12, 0, 0)
        idle_at = (now - timedelta(hours=5)).isoformat()
        metrics = {
            "session_id": "99999999-9999-9999-9999-999999999999",
            "pressure_ratio": 0.10,
            "user_turns": 3,
            "started_at": (now - timedelta(hours=30)).isoformat(),
            "last_user_at": idle_at,
        }
        state = {
            "last_activity": idle_at,
            "last_reset": (now - timedelta(hours=30)).isoformat(),
            "last_daily_reset_date": now.date().isoformat(),
        }
        decision = session_manager.evaluate_reset(state, metrics, now)
        self.assertEqual(decision["reason"], "age_reset")
        self.assertEqual(decision["action"], "rotate")
        with mock.patch.object(session_manager, "compact_mode_enabled", return_value=False):
            decision = session_manager.evaluate_reset(state, metrics, now)
            self.assertEqual(decision["reason"], "age_reset")

    @unittest.skipIf(fcntl is None, "fcntl is POSIX-only")
    def test_stale_lock_file_does_not_hold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            locks = Path(temp_dir)
            (locks / "bgtask.lock").write_text("left behind by a dead job\n")
            self.assertFalse(session_manager.has_held_lock(locks))

    @unittest.skipIf(fcntl is None, "fcntl is POSIX-only")
    def test_held_lock_holds_until_released(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            locks = Path(temp_dir)
            lock_path = locks / "bgtask.lock"
            lock_path.write_text("")
            with lock_path.open("rb") as holder:
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(session_manager.has_held_lock(locks))
            self.assertFalse(session_manager.has_held_lock(locks))


if __name__ == "__main__":
    unittest.main()


class CompactGuardTests(unittest.TestCase):
    """2026-09-20: compaction must not be sent to a bridge without a live ACP child,
    and an empty-session refusal is a skip, not a failure."""

    def test_compact_skips_when_no_live_acp_session(self):
        with mock.patch.object(session_manager, "is_acp_turn_busy", return_value=False), \
             mock.patch.object(session_manager, "is_acp_session_live", return_value=False), \
             mock.patch.object(session_manager, "prepare_checkpoint") as prepare, \
             mock.patch.object(session_manager, "send_compact_command") as send, \
             mock.patch.object(session_manager, "save_state") as save:
            ok = session_manager.compact_session(
                {}, {"session_id": "x", "compact_count": 0}, {"reason": "context_pressure", "idle_minutes": 12}
            )
        self.assertFalse(ok)
        prepare.assert_not_called()
        send.assert_not_called()
        save.assert_not_called()

    def test_no_messages_to_compact_is_a_skip_not_a_failure(self):
        state = {"compact_fail_count": 2}
        saved = []
        with mock.patch.object(session_manager, "is_acp_turn_busy", return_value=False), \
             mock.patch.object(session_manager, "is_acp_session_live", return_value=True), \
             mock.patch.object(session_manager, "prepare_checkpoint", return_value=Path("/nonexistent/pending.json")), \
             mock.patch.object(session_manager, "commit_checkpoint", return_value={}), \
             mock.patch.object(session_manager, "write_session_handoff"), \
             mock.patch.object(session_manager, "log_restart"), \
             mock.patch.object(session_manager, "get_last_activity", return_value=0.0), \
             mock.patch.object(session_manager, "current_session_metrics", return_value={"session_id": "x", "compact_count": 0}), \
             mock.patch.object(session_manager, "send_compact_command",
                               return_value=(False, "assistant unavailable: Home Agent: /compact failed: Error: No messages to compact")), \
             mock.patch.object(session_manager, "load_state", return_value=state), \
             mock.patch.object(session_manager, "save_state", side_effect=lambda s: saved.append(dict(s))), \
             mock.patch.object(session_manager.time, "sleep"):
            ok = session_manager.compact_session(
                state, {"session_id": "x", "compact_count": 0}, {"reason": "context_pressure", "idle_minutes": 12}
            )
        self.assertTrue(ok)
        self.assertTrue(all(item.get("compact_fail_count") == 2 for item in saved))

    def test_bridge_error_detail_is_surfaced(self):
        class FakeSocket:
            def __init__(self, *a, **k): self.sent = b""
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def settimeout(self, t): pass
            def connect(self, p): pass
            def sendall(self, b): self.sent += b
            def recv(self, n):
                if self.sent:
                    self.sent = b""
                    return b'{"error":"assistant unavailable","detail":"/compact failed: no completion event within 300s"}\n'
                return b""
        with mock.patch.object(session_manager.socket, "socket", FakeSocket):
            ok, response = session_manager.send_compact_command(timeout=1)
        self.assertFalse(ok)
        self.assertIn("no completion event within 300s", response)
