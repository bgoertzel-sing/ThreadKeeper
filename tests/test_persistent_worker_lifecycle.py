"""Provider-free truth-table tests for persistent-worker lifecycle v1."""

import os
import re
import sys
import json
import tempfile
import unittest
from unittest import mock


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

    def test_manifest_budget_limits_are_strict_positive_integers(self):
        for budgets in (
            {"max_attempts": 0}, {"max_tool_calls": True},
            {"unknown_limit": 1},
        ):
            manifest = self.manifest()
            manifest["budgets"] = budgets
            with self.assertRaisesRegex(ValueError, "budget"):
                lifecycle.create_task_manifest(self.root, manifest)

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

    def queued_result(self, task_id, *_args):
        return json.dumps({
            "status": "queued",
            "queue_sha256": "a" * 64,
            "queue_path": f"queue/persistent-{task_id}.json",
        })

    def waiting_input_fixture(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        for event_id, expected, prior, target in (
            ("queued-1", 0, "CREATED", "QUEUED"),
            ("claim-1", 1, "QUEUED", "CLAIMED"),
            ("running-1", 2, "CLAIMED", "RUNNING"),
            ("waiting-1", 3, "RUNNING", "WAITING_INPUT"),
        ):
            lifecycle.append_task_event(
                self.root, "task-1", event_id=event_id,
                expected_version=expected, prior_state=prior,
                new_state=target, actor="worker",
            )

    def test_inbox_item_is_bounded_immutable_and_restart_readable(self):
        self.waiting_input_fixture()
        item = lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-1",
            source_event_id="waiting-1", actor="parent",
            payload={"message": "continue", "correlation_id": "msg-1"},
            created_at="2026-07-15T12:10:00+00:00",
        )
        replay = lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-1",
            source_event_id="waiting-1", actor="parent",
            payload={"message": "continue", "correlation_id": "msg-1"},
            created_at="2026-07-15T12:11:00+00:00",
        )
        self.assertEqual(replay, item)
        self.assertEqual(lifecycle.list_inbox_items(self.root, "task-1"), [item])
        self.assertEqual(item["sequence"], 1)
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "WAITING_INPUT")

    def test_inbox_rejects_ineligible_or_stale_source_event(self):
        lifecycle.create_task_manifest(self.root, self.manifest())
        with self.assertRaisesRegex(ValueError, "not waiting"):
            lifecycle.record_inbox_item(
                self.root, "task-1", item_id="input-1",
                source_event_id="waiting-1", actor="parent", payload={},
            )
        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        self.waiting_input_fixture()
        with self.assertRaisesRegex(ValueError, "source event mismatch"):
            lifecycle.record_inbox_item(
                self.root, "task-1", item_id="input-1",
                source_event_id="running-1", actor="parent", payload={},
            )

    def test_inbox_duplicate_conflict_and_tampering_fail_closed(self):
        self.waiting_input_fixture()
        lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-1",
            source_event_id="waiting-1", actor="parent", payload={"value": 1},
        )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            lifecycle.record_inbox_item(
                self.root, "task-1", item_id="input-1",
                source_event_id="waiting-1", actor="parent",
                payload={"value": 2},
            )
        path = lifecycle._inbox_item_path(self.root, "task-1", "input-1")
        with open(path, "r", encoding="utf-8") as source:
            item = json.load(source)
        item["payload"]["value"] = 99
        with open(path, "w", encoding="utf-8") as output:
            json.dump(item, output)
        with self.assertRaisesRegex(ValueError, "integrity"):
            lifecycle.list_inbox_items(self.root, "task-1")

    def test_inbox_consumption_requeues_once_with_verified_receipt(self):
        self.waiting_input_fixture()
        item = lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-1",
            source_event_id="waiting-1", actor="parent",
            payload={"message": "continue"},
        )
        calls = []

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        consumed = lifecycle.consume_inbox_item(
            self.root, "task-1", consumption_id="consume-1",
            item_id="input-1", enqueue=enqueue,
        )
        replay = lifecycle.consume_inbox_item(
            self.root, "task-1", consumption_id="consume-1",
            item_id="input-1", enqueue=enqueue,
        )
        self.assertEqual(consumed, replay)
        self.assertEqual(consumed["task"]["state"], "QUEUED")
        self.assertEqual(consumed["receipt"]["inbox_sha256"],
                         item["inbox_sha256"])
        self.assertEqual(len(calls), 1)
        self.assertIn("Verified persistent-worker inbox input", calls[0][1])
        self.assertIn(item["inbox_sha256"], calls[0][1])
        self.assertIn('"message":"continue"', calls[0][1])
        with open(os.path.join(self.root, "tasks", "task-1", "events.jsonl"),
                  "r", encoding="utf-8") as source:
            event = json.loads(source.readlines()[-1])
        self.assertEqual(event["event_id"], "consume-1")
        self.assertEqual(event["payload_sha256"],
                         consumed["receipt"]["receipt_sha256"])

    def test_inbox_consumption_receipt_closes_enqueue_event_crash_window(self):
        self.waiting_input_fixture()
        lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-1",
            source_event_id="waiting-1", actor="parent", payload={},
        )
        calls = []
        real_append = lifecycle.append_task_event

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        with mock.patch.object(
            lifecycle, "append_task_event", side_effect=RuntimeError("crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                lifecycle.consume_inbox_item(
                    self.root, "task-1", consumption_id="consume-1",
                    item_id="input-1", enqueue=enqueue,
                )
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "WAITING_INPUT")
        with mock.patch.object(lifecycle, "append_task_event", wraps=real_append):
            result = lifecycle.consume_inbox_item(
                self.root, "task-1", consumption_id="consume-1",
                item_id="input-1", enqueue=enqueue,
            )
        self.assertEqual(result["task"]["state"], "QUEUED")
        self.assertEqual(len(calls), 1)

    def test_inbox_consumption_conflict_and_tampering_fail_closed(self):
        self.waiting_input_fixture()
        lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-1",
            source_event_id="waiting-1", actor="parent", payload={"value": 1},
        )
        lifecycle.record_inbox_item(
            self.root, "task-1", item_id="input-2",
            source_event_id="waiting-1", actor="parent", payload={"value": 2},
        )
        real_append = lifecycle.append_task_event
        with mock.patch.object(
            lifecycle, "append_task_event", side_effect=RuntimeError("crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                lifecycle.consume_inbox_item(
                    self.root, "task-1", consumption_id="consume-1",
                    item_id="input-1", enqueue=self.queued_result,
                )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            lifecycle.consume_inbox_item(
                self.root, "task-1", consumption_id="consume-1",
                item_id="input-2", enqueue=lambda *_args: self.fail(
                    "queue effect repeated"
                ),
            )
        path = lifecycle._inbox_consumption_receipt_path(
            self.root, "task-1", "consume-1"
        )
        with open(path, "r", encoding="utf-8") as source:
            receipt = json.load(source)
        receipt["queue_sha256"] = "b" * 64
        with open(path, "w", encoding="utf-8") as output:
            json.dump(receipt, output)
        with mock.patch.object(lifecycle, "append_task_event", wraps=real_append):
            with self.assertRaisesRegex(ValueError, "integrity"):
                lifecycle.consume_inbox_item(
                    self.root, "task-1", consumption_id="consume-1",
                    item_id="input-1", enqueue=lambda *_args: self.fail(
                        "queue effect repeated"
                    ),
                )

    def test_spawn_is_provider_free_idempotent_and_uses_enqueue_seam(self):
        calls = []

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        first = lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1", enqueue=enqueue
        )
        replay = lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1", enqueue=enqueue
        )
        self.assertEqual(first, replay)
        self.assertEqual((first["state"], first["version"]), ("QUEUED", 1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:4], (
            "task-1", "Provider-free fixture", "read-file", "researcher"
        ))

    def test_failed_enqueue_remains_created_and_can_retry(self):
        with self.assertRaisesRegex(ValueError, "did not produce"):
            lifecycle.spawn_persistent(
                self.root, self.manifest(), spawn_id="spawn-1",
                enqueue=lambda *_args: json.dumps({"status": "error"}),
            )
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"], "CREATED")
        status = lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        self.assertEqual(status["state"], "QUEUED")

    def test_spawn_receipt_closes_enqueue_event_crash_window(self):
        calls = []
        real_append = lifecycle.append_task_event

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        with mock.patch.object(
            lifecycle, "append_task_event", side_effect=RuntimeError("crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                lifecycle.spawn_persistent(
                    self.root, self.manifest(), spawn_id="spawn-1", enqueue=enqueue
                )
        receipt = lifecycle._read_enqueue_receipt(self.root, "task-1", "spawn-1")
        self.assertEqual(receipt["queue_sha256"], "a" * 64)
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"], "CREATED")

        with mock.patch.object(lifecycle, "append_task_event", wraps=real_append):
            status = lifecycle.spawn_persistent(
                self.root, self.manifest(), spawn_id="spawn-1", enqueue=enqueue
            )
        self.assertEqual(status["state"], "QUEUED")
        self.assertEqual(len(calls), 1)

    def test_corrupt_spawn_receipt_fails_closed_without_reenqueue(self):
        calls = []

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        with mock.patch.object(
            lifecycle, "append_task_event", side_effect=RuntimeError("crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                lifecycle.spawn_persistent(
                    self.root, self.manifest(), spawn_id="spawn-1", enqueue=enqueue
                )
        path = lifecycle._enqueue_receipt_path(self.root, "task-1", "spawn-1")
        with open(path, "r", encoding="utf-8") as source:
            receipt = json.load(source)
        receipt["queue_sha256"] = "bad"
        with open(path, "w", encoding="utf-8") as output:
            json.dump(receipt, output)
        with self.assertRaisesRegex(ValueError, "queue digest invalid"):
            lifecycle.spawn_persistent(
                self.root, self.manifest(), spawn_id="spawn-1", enqueue=enqueue
            )
        self.assertEqual(len(calls), 1)

    def test_cancel_is_idempotent_and_prevents_later_claim_or_effect(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        interventions = []
        cancelled = lifecycle.cancel_persistent(
            self.root, "task-1", cancel_id="cancel-1",
            request_cancel=lambda task_id: interventions.append(task_id),
        )
        replay = lifecycle.cancel_persistent(
            self.root, "task-1", cancel_id="cancel-1",
            request_cancel=lambda task_id: interventions.append("duplicate"),
        )
        effects = []
        result = lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1",
            cancel_present=lambda _task_id: True,
            run_queued=lambda path, checkpoint: effects.append((path, checkpoint)),
        )
        self.assertEqual(cancelled, replay)
        self.assertEqual(interventions, ["task-1"])
        self.assertEqual(result["status"], "not_claimed")
        self.assertEqual(effects, [])
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "CANCEL_REQUESTED")

    def test_cancel_token_observed_before_claim_prevents_effect(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        effects = []
        result = lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1",
            cancel_present=lambda _task_id: True,
            run_queued=lambda path, checkpoint: effects.append((path, checkpoint)),
        )
        self.assertEqual(result["status"], "cancelled_before_claim")
        self.assertEqual(effects, [])
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"], "QUEUED")

    def test_claim_cas_precedes_queue_effect(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        observations = []

        def run_queue(path, checkpoint):
            observations.append((path, checkpoint, lifecycle.worker_status(self.root, "task-1")["state"]))
            return "provider-free-result"

        result = lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1",
            cancel_present=lambda _task_id: False, run_queued=run_queue,
        )
        self.assertEqual(result["status"], "claimed")
        self.assertEqual(observations, [("persistent-task-1.json", None, "CLAIMED")])
        self.assertEqual(result["attempt"]["attempt_id"], "claim-1")
        self.assertEqual(len(lifecycle.list_attempts(self.root, "task-1")), 1)

    def test_attempt_lease_is_immutable_and_required_before_effect(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        effects = []
        result = lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1", attempt_id="attempt-1",
            worker_id="worker-1", lease_seconds=60,
            cancel_present=lambda _task_id: False,
            run_queued=lambda path, checkpoint: effects.append((path, checkpoint)) or "done",
        )
        attempt = result["attempt"]
        self.assertEqual(attempt["claim_event_id"], "claim-1")
        self.assertEqual(attempt["worker_id"], "worker-1")
        self.assertEqual(effects, [("persistent-task-1.json", None)])
        path = os.path.join(
            self.root, "tasks", "task-1", "attempts", "attempt-1.json"
        )
        with open(path, "r", encoding="utf-8") as source:
            tampered = json.load(source)
        tampered["worker_id"] = "forged"
        with open(path, "w", encoding="utf-8") as output:
            json.dump(tampered, output)
        with self.assertRaisesRegex(ValueError, "integrity"):
            lifecycle.list_attempts(self.root, "task-1")

    def test_budget_usage_is_durable_idempotent_and_hash_chained(self):
        manifest = self.manifest()
        manifest["budgets"].update({
            "max_total_tokens": 100, "max_tool_calls": 3,
        })
        lifecycle.spawn_persistent(
            self.root, manifest, spawn_id="spawn-1", enqueue=self.queued_result,
        )
        lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1", attempt_id="attempt-1",
            cancel_present=lambda _task_id: False,
            run_queued=lambda _path, _checkpoint: "provider-free",
        )
        first = lifecycle.record_budget_usage(
            self.root, "task-1", usage_id="usage-1", attempt_id="attempt-1",
            counters={"total_tokens": 40, "tool_calls": 1},
            created_at="2026-07-15T12:03:00+00:00",
        )
        replay = lifecycle.record_budget_usage(
            self.root, "task-1", usage_id="usage-1", attempt_id="attempt-1",
            counters={"total_tokens": 40, "tool_calls": 1},
            created_at="2026-07-15T12:04:00+00:00",
        )
        second = lifecycle.record_budget_usage(
            self.root, "task-1", usage_id="usage-2", attempt_id="attempt-1",
            counters={"total_tokens": 60},
            created_at="2026-07-15T12:05:00+00:00",
        )
        self.assertEqual(replay, first)
        self.assertEqual(second["previous_budget_event_sha256"],
                         first["budget_event_sha256"])
        status = lifecycle.budget_status(self.root, "task-1")
        self.assertEqual(status["consumed"]["total_tokens"], 100)
        self.assertEqual(status["consumed"]["tool_calls"], 1)
        self.assertEqual(status["event_count"], 2)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["exhausted"], ["max_total_tokens"])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            lifecycle.record_budget_usage(
                self.root, "task-1", usage_id="usage-1",
                attempt_id="attempt-1", counters={"total_tokens": 41},
            )

    def test_completed_queue_result_is_accounted_once_across_ledger_crash(self):
        manifest = self.manifest()
        manifest["budgets"]["max_total_tokens"] = 100
        lifecycle.spawn_persistent(
            self.root, manifest, spawn_id="spawn-1", enqueue=self.queued_result,
        )
        calls = []
        queue_result = json.dumps({
            "status": "ok",
            "result": {"worker_token_usage": {
                "input_tokens": 7, "output_tokens": 5, "total_tokens": 12,
            }},
        })
        real_record = lifecycle.record_budget_usage
        with mock.patch.object(
            lifecycle, "record_budget_usage", side_effect=RuntimeError("crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                lifecycle.run_persistent_queued_dispatch(
                    self.root, "task-1", claim_id="claim-1",
                    attempt_id="attempt-1",
                    cancel_present=lambda _task_id: False,
                    run_queued=lambda path, checkpoint: calls.append(
                        (path, checkpoint)
                    ) or queue_result,
                )

        with mock.patch.object(lifecycle, "record_budget_usage", wraps=real_record):
            replay = lifecycle.run_persistent_queued_dispatch(
                self.root, "task-1", claim_id="claim-1",
                attempt_id="attempt-1",
                cancel_present=lambda _task_id: False,
                run_queued=lambda *_args: self.fail("queue effect repeated"),
            )
        self.assertEqual(len(calls), 1)
        self.assertTrue(replay["receipt_replayed"])
        self.assertEqual(replay["queue_result"], queue_result)
        budget = lifecycle.budget_status(self.root, "task-1")
        self.assertEqual(budget["consumed"]["input_tokens"], 7)
        self.assertEqual(budget["consumed"]["output_tokens"], 5)
        self.assertEqual(budget["consumed"]["total_tokens"], 12)
        self.assertEqual(budget["event_count"], 1)

    def test_queue_result_accounting_rejects_inconsistent_token_totals(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        bad_result = json.dumps({
            "status": "ok",
            "result": {"worker_token_usage": {
                "input_tokens": 7, "output_tokens": 5, "total_tokens": 13,
            }},
        })
        with self.assertRaisesRegex(ValueError, "total mismatch"):
            lifecycle.run_persistent_queued_dispatch(
                self.root, "task-1", claim_id="claim-1",
                attempt_id="attempt-1",
                cancel_present=lambda _task_id: False,
                run_queued=lambda *_args: bad_result,
            )
        receipt_path = lifecycle._attempt_result_receipt_path(
            self.root, "task-1", "attempt-1"
        )
        self.assertFalse(os.path.exists(receipt_path))

    def test_attempt_result_receipt_tampering_fails_closed(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1",
            attempt_id="attempt-1", cancel_present=lambda _task_id: False,
            run_queued=lambda *_args: json.dumps({
                "status": "ok",
                "result": {"worker_token_usage": {
                    "input_tokens": 1, "output_tokens": 1,
                    "total_tokens": 2,
                }},
            }),
        )
        path = lifecycle._attempt_result_receipt_path(
            self.root, "task-1", "attempt-1"
        )
        with open(path, "r", encoding="utf-8") as source:
            receipt = json.load(source)
        receipt["counters"]["total_tokens"] = 99
        with open(path, "w", encoding="utf-8") as output:
            json.dump(receipt, output)
        with self.assertRaisesRegex(ValueError, "integrity"):
            lifecycle.run_persistent_queued_dispatch(
                self.root, "task-1", claim_id="claim-1",
                attempt_id="attempt-1",
                cancel_present=lambda _task_id: False,
                run_queued=lambda *_args: self.fail("queue effect repeated"),
            )

    def test_budget_exhaustion_blocks_requeue_before_queue_effect(self):
        manifest = self.manifest()
        manifest["budgets"] = {"max_attempts": 1}
        lifecycle.spawn_persistent(
            self.root, manifest, spawn_id="spawn-1", enqueue=self.queued_result,
        )
        lifecycle.append_task_event(
            self.root, "task-1", event_id="claim-1", expected_version=1,
            prior_state="QUEUED", new_state="CLAIMED", actor="worker",
        )
        lifecycle.create_attempt(
            self.root, "task-1", attempt_id="attempt-1",
            claim_event_id="claim-1", worker_id="worker-1",
            created_at="2026-07-15T12:00:00+00:00",
            lease_expires_at="2026-07-15T12:05:00+00:00",
        )
        lifecycle.recover_stale_attempt(
            self.root, "task-1", recovery_id="recovery-1",
            now="2026-07-15T12:05:01+00:00",
        )
        effects = []
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            lifecycle.requeue_persistent(
                self.root, "task-1", requeue_id="requeue-1",
                enqueue=lambda *_args: effects.append(True),
            )
        self.assertEqual(effects, [])
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "FAILED_RETRYABLE")

    def test_invalid_lease_fails_before_claim_mutation(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        effects = []
        with self.assertRaisesRegex(ValueError, "lease seconds"):
            lifecycle.run_persistent_queued_dispatch(
                self.root, "task-1", claim_id="claim-1", lease_seconds=0,
                cancel_present=lambda _task_id: False,
                run_queued=lambda path, checkpoint: effects.append((path, checkpoint)),
            )
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "QUEUED")
        self.assertEqual(effects, [])

    def test_checkpoint_chain_is_bounded_idempotent_and_integrity_checked(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-1", attempt_id="attempt-1",
            cancel_present=lambda _task_id: False,
            run_queued=lambda _path, _checkpoint: "paused",
        )
        lifecycle.append_task_event(
            self.root, "task-1", event_id="running-1", expected_version=2,
            prior_state="CLAIMED", new_state="RUNNING", actor="worker",
        )
        first = lifecycle.create_checkpoint(
            self.root, "task-1", checkpoint_id="checkpoint-1",
            attempt_id="attempt-1", payload={"cursor": 3, "digest": "safe"},
            created_at="2026-07-15T12:03:00+00:00",
        )
        replay = lifecycle.create_checkpoint(
            self.root, "task-1", checkpoint_id="checkpoint-1",
            attempt_id="attempt-1", payload={"cursor": 3, "digest": "safe"},
            created_at="2026-07-15T12:03:00+00:00",
        )
        second = lifecycle.create_checkpoint(
            self.root, "task-1", checkpoint_id="checkpoint-2",
            attempt_id="attempt-1", payload={"cursor": 7},
            created_at="2026-07-15T12:04:00+00:00",
        )
        self.assertEqual(first, replay)
        self.assertEqual(second["previous_checkpoint_sha256"],
                         first["checkpoint_sha256"])
        self.assertEqual([item["sequence"] for item in
                          lifecycle.read_checkpoint_chain(self.root, "task-1")],
                         [1, 2])
        path = os.path.join(
            self.root, "tasks", "task-1", "checkpoints", "checkpoint-2.json"
        )
        with open(path, "r", encoding="utf-8") as source:
            tampered = json.load(source)
        tampered["payload"]["cursor"] = 99
        with open(path, "w", encoding="utf-8") as output:
            json.dump(tampered, output)
        with self.assertRaisesRegex(ValueError, "payload integrity"):
            lifecycle.read_checkpoint_chain(self.root, "task-1")

    def test_restart_recovery_requires_expired_verified_attempt(self):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        lifecycle.append_task_event(
            self.root, "task-1", event_id="claim-1", expected_version=1,
            prior_state="QUEUED", new_state="CLAIMED", actor="worker",
        )
        lifecycle.create_attempt(
            self.root, "task-1", attempt_id="attempt-1",
            claim_event_id="claim-1", worker_id="worker-1",
            created_at="2026-07-15T12:00:00+00:00",
            lease_expires_at="2026-07-15T12:05:00+00:00",
        )
        active = lifecycle.recovery_assessment(
            self.root, "task-1", now="2026-07-15T12:04:59+00:00"
        )
        self.assertFalse(active["recoverable"])
        recovered = lifecycle.recover_stale_attempt(
            self.root, "task-1", recovery_id="recovery-1",
            now="2026-07-15T12:05:01+00:00",
        )
        self.assertTrue(recovered["recoverable"])
        self.assertEqual(recovered["state"], "FAILED_RETRYABLE")
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "FAILED_RETRYABLE")
        replay = lifecycle.recover_stale_attempt(
            self.root, "task-1", recovery_id="recovery-1",
            now="2026-07-15T12:05:02+00:00",
        )
        self.assertEqual(replay, recovered)

    def _failed_retryable_fixture(self, checkpoint=False):
        lifecycle.spawn_persistent(
            self.root, self.manifest(), spawn_id="spawn-1",
            enqueue=self.queued_result,
        )
        lifecycle.append_task_event(
            self.root, "task-1", event_id="claim-1", expected_version=1,
            prior_state="QUEUED", new_state="CLAIMED", actor="worker",
        )
        lifecycle.create_attempt(
            self.root, "task-1", attempt_id="attempt-1",
            claim_event_id="claim-1", worker_id="worker-1",
            created_at="2026-07-15T12:00:00+00:00",
            lease_expires_at="2026-07-15T12:05:00+00:00",
        )
        if checkpoint:
            lifecycle.append_task_event(
                self.root, "task-1", event_id="running-1", expected_version=2,
                prior_state="CLAIMED", new_state="RUNNING", actor="worker",
            )
            lifecycle.create_checkpoint(
                self.root, "task-1", checkpoint_id="checkpoint-1",
                attempt_id="attempt-1", payload={"cursor": 3},
                created_at="2026-07-15T12:04:00+00:00",
            )
        lifecycle.recover_stale_attempt(
            self.root, "task-1", recovery_id="recovery-1",
            now="2026-07-15T12:05:01+00:00",
        )

    def test_explicit_requeue_is_idempotent_and_records_queue_digest(self):
        self._failed_retryable_fixture(checkpoint=True)
        calls = []

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        queued = lifecycle.requeue_persistent(
            self.root, "task-1", requeue_id="requeue-1", enqueue=enqueue,
        )
        replay = lifecycle.requeue_persistent(
            self.root, "task-1", requeue_id="requeue-1", enqueue=enqueue,
        )
        self.assertEqual((queued["state"], queued["version"]), ("QUEUED", 5))
        self.assertEqual(replay, queued)
        self.assertEqual(len(calls), 1)
        with open(os.path.join(self.root, "tasks", "task-1", "events.jsonl"),
                  "r", encoding="utf-8") as source:
            event = json.loads(source.readlines()[-1])
        self.assertEqual(event["event_id"], "requeue-1")
        self.assertEqual(event["payload_sha256"], "a" * 64)

    def test_requeued_attempt_receives_verified_checkpoint(self):
        self._failed_retryable_fixture(checkpoint=True)
        lifecycle.requeue_persistent(
            self.root, "task-1", requeue_id="requeue-1",
            enqueue=self.queued_result,
        )
        observed = []
        claimed = lifecycle.run_persistent_queued_dispatch(
            self.root, "task-1", claim_id="claim-2", attempt_id="attempt-2",
            cancel_present=lambda _task_id: False,
            run_queued=lambda path, checkpoint: observed.append(
                (path, checkpoint)
            ) or "resumed",
        )
        checkpoint = lifecycle.read_checkpoint_chain(self.root, "task-1")[-1]
        self.assertEqual(observed, [("persistent-task-1.json", checkpoint)])
        self.assertEqual(claimed["resume_checkpoint"], checkpoint)
        self.assertEqual(claimed["attempt"]["resume_checkpoint_id"],
                         checkpoint["checkpoint_id"])
        self.assertEqual(claimed["attempt"]["resume_checkpoint_sha256"],
                         checkpoint["checkpoint_sha256"])

    def test_failed_requeue_effect_does_not_change_retryable_state(self):
        self._failed_retryable_fixture()
        with self.assertRaisesRegex(ValueError, "did not produce"):
            lifecycle.requeue_persistent(
                self.root, "task-1", requeue_id="requeue-1",
                enqueue=lambda *_args: {"status": "error"},
            )
        self.assertEqual(lifecycle.worker_status(self.root, "task-1")["state"],
                         "FAILED_RETRYABLE")

    def test_requeue_receipt_closes_enqueue_event_crash_window(self):
        self._failed_retryable_fixture()
        calls = []
        real_append = lifecycle.append_task_event

        def enqueue(*args):
            calls.append(args)
            return self.queued_result(*args)

        with mock.patch.object(
            lifecycle, "append_task_event", side_effect=RuntimeError("crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                lifecycle.requeue_persistent(
                    self.root, "task-1", requeue_id="requeue-1", enqueue=enqueue
                )
        self.assertEqual(
            lifecycle.worker_status(self.root, "task-1")["state"], "FAILED_RETRYABLE"
        )
        with mock.patch.object(lifecycle, "append_task_event", wraps=real_append):
            status = lifecycle.requeue_persistent(
                self.root, "task-1", requeue_id="requeue-1", enqueue=enqueue
            )
        self.assertEqual(status["state"], "QUEUED")
        self.assertEqual(len(calls), 1)

    def test_requeue_rejects_corrupt_checkpoint_before_enqueue(self):
        self._failed_retryable_fixture(checkpoint=True)
        path = os.path.join(
            self.root, "tasks", "task-1", "checkpoints", "checkpoint-1.json"
        )
        with open(path, "r", encoding="utf-8") as source:
            record = json.load(source)
        record["payload"]["cursor"] = 99
        with open(path, "w", encoding="utf-8") as output:
            json.dump(record, output)
        calls = []
        with self.assertRaisesRegex(ValueError, "payload integrity"):
            lifecycle.requeue_persistent(
                self.root, "task-1", requeue_id="requeue-1",
                enqueue=lambda *_args: calls.append(True),
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
