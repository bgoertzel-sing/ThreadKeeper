"""Provider-free truth-table tests for persistent-worker lifecycle v1."""

import os
import re
import sys
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


if __name__ == "__main__":
    unittest.main()
