import unittest
from pathlib import Path


class EstimateAllocationWorkflow(unittest.TestCase):
    def test_allocation_downloads_only_successful_estimate_from_same_run(self):
        workflow = (Path(__file__).parents[1] / "examples/prism-estimate.yml").read_text()
        estimate, allocation = workflow.split("  allocate:\n", 1)
        self.assertIn("estimated: ${{ steps.estimate.outcome == 'success' }}", estimate)
        self.assertIn("name: prism-estimate-${{ github.event.issue.number }}", estimate)
        self.assertIn("needs: estimate", allocation)
        self.assertIn("if: github.repository == 'Bhavya031/k-2' && needs.estimate.outputs.estimated == 'true'", allocation)
        self.assertIn("RUN_ID: ${{ github.run_id }}", allocation)
        self.assertIn("ISSUE_NUMBER: ${{ github.event.issue.number }}", allocation)
        self.assertIn('gh run download "$RUN_ID" --repo "$GITHUB_REPOSITORY" --name "prism-estimate-$ISSUE_NUMBER"', allocation)
        self.assertIn('python -m prism.cfo_actions "$RUNNER_TEMP/prism-estimate/estimate.json" "$RUN_ID"', allocation)
        self.assertIn('group: prism-cfo-allocation\n      cancel-in-progress: false', allocation)
        self.assertLess(allocation.index("Check GitHub secret read and write access before CFO OAuth"),
                        allocation.index("Allocate in the synthetic CFO.ai tables"))


if __name__ == "__main__":
    unittest.main()
