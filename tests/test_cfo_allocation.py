import unittest

from prism.cfo_allocation import decide


REQUEST = {"request_id": "k2-40-run-38135685609", "repository": "Bhavya031/k-2", "estimated_usd": 1.40415,
           "issue_url": "https://github.com/Bhavya031/k-2/issues/40", "pinned_commit": "35adaf58a7c2b577a85f6dca30fb73f1005452c5",
           "model": "claude-sonnet-5-5", "predicted_tokens": {"input": 636519, "uncached_input": 636519,
                                               "cached_input": 0, "output": 11400, "total": 647919}}
BUDGET = {"repository": "Bhavya031/k-2", "budget_usd": 25, "spending_usd": 0, "existing_reservations_usd": 0}
POLICY = {"max_budget_fraction": 0.2}


class CFOAllocation(unittest.TestCase):
    def test_allocate_and_repeat_one_reservation(self):
        first, row = decide(REQUEST, BUDGET, POLICY, [])
        self.assertEqual((first["decision"], first["allocated_usd_cap"], first["available_balance"]),
                         ("allocated", 1.40415, 23.59585))
        self.assertEqual(row["reservation_usd"], 1.40415)
        repeat, second = decide(REQUEST, BUDGET, POLICY, [row])
        self.assertEqual(repeat, first)
        self.assertIsNone(second)

    def test_missing_budget_or_policy_blocks(self):
        for budget, policy in ((None, POLICY), (BUDGET, None)):
            result, row = decide(REQUEST, budget, policy, [])
            self.assertEqual(result["decision"], "blocked")
            self.assertIsNone(row)

    def test_policy_cap_denies_without_reservation(self):
        result, row = decide(REQUEST | {"estimated_usd": 6}, BUDGET, POLICY, [])
        self.assertEqual((result["decision"], result["available_balance"]), ("denied", 25))
        self.assertIsNone(row)

    def test_other_repository_reservations_do_not_reduce_k2_balance(self):
        other = {"request_id": "other", "repository": "elsewhere", "decision": "allocated", "reservation_usd": 20}
        result, _ = decide(REQUEST, BUDGET, POLICY, [other])
        self.assertEqual(result["available_balance"], 23.59585)
