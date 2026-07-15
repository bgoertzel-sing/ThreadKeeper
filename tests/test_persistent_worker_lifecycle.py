"""Provider-free truth-table tests for persistent-worker lifecycle v1."""

import os
import re
import sys
import json
import tempfile
import unittest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import persistent_worker as lifecycle  # noqa: E402


class PersistentWorkerLifecycleTests(unittest.TestCase):
    def test_complete_state_pair_truth_table(self):
        for from_state in lifecycle.STATES:
            for to_state in lifecycle.STATES:
                decision = lifecycle.transition_decision(from_state, to_state)
                self.assertEqual(
                    decision.allowed,
                    (from_state, to_state) in lifecycle.ALLOWED_TRANSITIONS,
                    (from_state, to_state, decision),
                )

    def test_terminal_states_have_no_outgoing_transitions(self):
        for from_state in lifecycle.TERMINAL_STATES:
            self.assertTrue(lifecycle.is_terminal(from_state))
            for to_state in lifecycle.STATES:
                self.assertFalse(
                    lifecycle.transition_decision(from_state, to_state).allowed
                )

    def test_unknown_and_malformed_states_fail_closed(self):
        for invalid in (None, True, 1, "", "RUNNING\nCOMPLETED", "UNKNOWN"):
            self.assertFalse(lifecycle.is_terminal(invalid))
            self.assertFalse(
                lifecycle.transition_decision(invalid, "QUEUED").allowed
            )
            self.assertFalse(
                lifecycle.transition_decision("QUEUED", invalid).allowed
            )

    def test_metta_policy_has_exact_python_transition_set(self):
        policy_path = os.path.join(
            _REPO, "src", "persistent_worker_lifecycle.metta"
        )
        with open(policy_path, "r", encoding="utf-8") as policy_file:
            policy = policy_file.read()
        metta_pairs = set(re.findall(
            r"\(and \(== \$from ([A-Z_]+)\) \(== \$to ([A-Z_]+)\)\)",
            policy,
        ))
        self.assertEqual(metta_pairs, set(lifecycle.ALLOWED_TRANSITIONS))
        self.assertEqual(policy.count("(= (pw-transition $from $to)"), 1)
        self.assertIn("ALLOW\n     DENY", policy)

    def test_metta_policy_has_exact_python_terminal_set(self):
        policy_path = os.path.join(
            _REPO, "src", "persistent_worker_lifecycle.metta"
        )
        with open(policy_path, "r", encoding="utf-8") as policy_file:
            policy = policy_file.read()
        terminal_block = policy.split("(= (pw-transition", 1)[0]
        metta_terminals = set(re.findall(
            r"\(== \$state ([A-Z_]+)\)", terminal_block
        ))
        self.assertEqual(metta_terminals, set(lifecycle.TERMINAL_STATES))
        self.assertEqual(policy.count("(= (pw-terminal $state)"), 1)


class PersistentWorkerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self, task_id="task-1"):
        return {
            "task_id": task_id,
            "deployment_id": "test-deployment",
            "created_at": "2026-07-15T12:00:00+00:00",
            "objective": "Provider-free fixture",
            "persona_key": "researcher",
            "tool_subset": ["read-file"],
            "task_contract": {"allowed_paths": ["scratch"]},
            "budgets": {"max_attempts": 2},
            "provenance": {"creator": "unit-test"},
        }

    def test_manifest_and_event_status_round_trip(self):
        manifest = lifecycle.create_task_manifest(self.root, self.manifest())
        self.assertEqual(manifest["manifest_version"], lifecycle.MANIFEST_VERSION)
        initial = lifecycle.worker_status(self.root, "task-1")
        self.assertEqual((initial["state"], initial["version"]), ("CREATED", 0))

        first = lifecycle.append_task_event(
            self.root, "task-1", event_id="event-1", expected_version=0,
            prior_state="CREATED", new_state="QUEUED", actor="test",
            timestamp="2026-07-15T12:01:00+00:00",
        )
        second = lifecycle.append_task_event(
            self.root, "task-1", event_id="event-2", expected_version=1,
            prior_state="QUEUED", new_state="CLAIMED", actor="test",
            timestamp="2026-07-15T12:02:00+00:00",
        )
        status = lifecycle.worker_status(self.root, "task-1")
        self.assertEqual((status["state"], status["version"]), ("CLAIMED", 2))
        self.assertEqual(status["event_count"], 2)
        self.assertEqual(second["previous_event_sha256"], first["event_sha256"])
        self.assertEqual(lifecycle.list_worker_statuses(self.root), [status])

    def test_event_replay_is_idempotent_and_cas_checked(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        event = lifecycle.append_task_event(
            self.root, "task-1", event_id="same-event", expected_version=0,
            prior_state="CREATED", new_state="QUEUED", actor="test",
        )
        replay = lifecycle.append_task_event(
            self.root, "task-1", event_id="same-event", expected_version=0,
            prior_state="CREATED", new_state="QUEUED", actor="test",
        )
        self.assertEqual(replay, event)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            lifecycle.append_task_event(
                self.root, "task-1", event_id="same-event", expected_version=0,
                prior_state="CREATED", new_state="CANCELLED", actor="test",
            )
        with self.assertRaisesRegex(ValueError, "compare-and-swap"):
            lifecycle.append_task_event(
                self.root, "task-1", event_id="new-event", expected_version=0,
                prior_state="CREATED", new_state="QUEUED", actor="test",
            )
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["event_count"], 1)

    def test_denied_transition_does_not_append(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        with self.assertRaisesRegex(ValueError, "transition denied"):
            lifecycle.append_task_event(
                self.root, "task-1", event_id="event-1", expected_version=0,
                prior_state="CREATED", new_state="RUNNING", actor="test",
            )
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["event_count"], 0)

    def test_tampered_manifest_or_event_fails_status_closed(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        lifecycle.append_task_event(
            self.root, "task-1", event_id="event-1", expected_version=0,
            prior_state="CREATED", new_state="QUEUED", actor="test",
        )
        events_path = os.path.join(self.root, "tasks", "task-1", "events.jsonl")
        with open(events_path, "r", encoding="utf-8") as source:
            event = json.loads(source.readline())
        event["new_state"] = "COMPLETED"
        with open(events_path, "w", encoding="utf-8") as output:
            output.write(json.dumps(event) + "\n")
        with self.assertRaisesRegex(ValueError, "integrity|transition"):
            lifecycle.worker_status(self.root, "task-1")

    def test_symlink_task_entry_fails_list_closed(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        os.symlink(os.path.join(self.root, "tasks", "task-1"),
                   os.path.join(self.root, "tasks", "forged"))
        with self.assertRaisesRegex(ValueError, "task entry"):
            lifecycle.list_worker_statuses(self.root)

    def test_unknown_versions_and_oversized_logs_fail_closed(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        manifest_path = os.path.join(self.root, "tasks", "task-1", "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as source:
            manifest = json.load(source)
        manifest["manifest_version"] = "future"
        with open(manifest_path, "w", encoding="utf-8") as output:
            json.dump(manifest, output)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            lifecycle.worker_status(self.root, "task-1")

        lifecycle.create_task_manifest(self.root, self.manifest("task-2"))
        events_path = os.path.join(self.root, "tasks", "task-2", "events.jsonl")
        with open(events_path, "wb") as output:
            output.truncate(lifecycle.MAX_EVENT_LOG_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            lifecycle.worker_status(self.root, "task-2")


if __name__ == "__main__":
    unittest.main()
