# Install Prism estimates on GitHub issues

1. Copy [the workflow](../.github/workflows/prism-estimate.yml) to `.github/workflows/prism-estimate.yml` in the repository where issues are opened. Copy it unchanged; it reads the repository and issue from the event. Set Actions workflow permissions to allow issue comments if repository settings override the YAML permissions.
2. Set these Actions secrets in that repository:
   - `ANTHROPIC_API_KEY`: required for the default Claude file prediction.
   - `ENTIRE_TOKEN`: read access to this repository in Entire. The workflow checks that its `gh/owner/repo` entry is ready before making model calls.
   - `PRISME_READ_TOKEN`: GitHub read access to the private `Bhavya031/Prisme` repository for installation.
   - `GLANCE_READ_TOKEN`: GitHub read access to the private `Bhavya031/Glance` repository for installation. One token may serve both private repositories if it has access to both.

   The built-in `GITHUB_TOKEN` reads this repository and updates issue comments; it needs the workflow's `contents: read` and `issues: write` permissions. No extra token is needed for those same-repository operations.
3. Connect this exact GitHub repository to Entire and index it until `gh/owner/repo` reports **ready**. Entire's repository name must correspond to the issue repository. An unregistered or unfinished index causes a setup failure before any model call. The workflow searches the pinned Git checkout, so a replay does not use hits from a newer Entire index.
4. Open an issue as an owner, member, or collaborator. To request a later estimate, post a comment containing exactly `/prism estimate`. Other comments, bot comments, pull request comments, and unauthorized authors do not start a run. The workflow serializes runs and keeps a repository-wide ten-minute cooldown; it updates one marked Prism comment on the issue.
5. For a historical replay, set the repository Actions variable `PRISM_REPLAY_REF` to a maintainer-chosen branch, tag, or commit. The workflow resolves it to a full commit at run start and uses that commit for search, checkout, file reading, token measurement, and the reported estimate. Remove the variable to use the repository default branch. Issue text cannot select the replay ref. Replaying old code with today's issue text or references can still differ from a historical issue snapshot.

The workflow pins Prism and Glance by commit. `PRISM_COMMIT` is `122ac0c94e2603a083ae13830ac135b075272fe2`, the local commit containing the new `--commit` support. **After these local commits are published, verify that this commit is reachable from `Bhavya031/Prisme` before copying the workflow.** `GLANCE_COMMIT` is pinned to `3465201a46801c96fe57958306e36eb3b3132679`, because no Glance release tag was found in the inspected remote refs.

Each successful run saves `predict.json`, `estimate.json`, and the comment as a workflow artifact. A setup or estimate failure appears in the workflow run and in the issue comment when comment posting is available. No solve worker is launched; the estimate is a heuristic built from file prediction and Glance token counting.
