# Set up Prism on a GitHub repository

From a Prisme checkout, run `bash scripts/setup-github.sh /path/to/your/repo`. You need `gh` authenticated for that repository and permission to set its Actions secrets. The command asks for every setting, hides secret input, writes `.github/prism.json` when using a per-repository config, and copies the two small caller files in `examples/` to `.github/workflows/`. Review and commit the two callers, plus `.github/prism.json` if generated, on the target repository's default branch. It stops if any destination file already exists. Prisme must be public before public forks can call its reusable workflows.

Before opening an issue, register and index the same `owner/repo` in Entire. [The existing install guide](install-github-estimate.md#entire-setup-and-current-credential-limit) gives the available CLI commands. The workflow requires `/gh/owner/repo` to report `ready` before it makes a model call. Set repository Actions permissions to allow the workflow's `issues: write`, `contents: read`, and `actions: read` declarations. An owner, member, or collaborator can open an issue or comment exactly `/prism estimate` to start an estimate; a successful estimate starts allocation.

## Values to provide

These secrets are passed explicitly by the caller workflows. Set them in the **target** repository's Settings → Secrets and variables → Actions → Repository secrets. The setup command asks for them without showing their values. `GITHUB_TOKEN` is supplied by Actions and uses the workflow permissions above; it is not a secret you provide. The preflight creates `CFO_SECRET_WRITE_PREFLIGHT` with the fixed value `ok`; you do not provide it.

| Secret | What it is | Where to get it | Required? |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Key for Claude prediction and estimation. | Anthropic API account. | Only when `estimate.provider` is `claude`. |
| `OPENAI_API_KEY` | Key for OpenAI prediction and estimation. | OpenAI API account. | Only when `estimate.provider` is `openai`. |
| `NEATLOGS_API_KEY` | Sends prediction traces to Neatlogs. | Neatlogs account; issuance steps are not documented in code. | No. Press Enter to skip. |
| `ENTIRE_TOKEN` | Entire credential for searching the indexed target repository. | Log in to Entire in the region containing the mirror and create a token there; the exact issuance flow is not documented in code. | Yes. |
| `PRISME_READ_TOKEN` | GitHub token that can read private `Bhavya031/Prisme`, used by install and checkout. | GitHub personal access token with Prisme Contents read permission. | Only while Prisme is private. |
| `CFO_OAUTH_CLIENT_ID` | CFO.ai OAuth client ID used with the refresh token. | CFO.ai OAuth registration; obtaining it is not documented in code. | Yes for allocation. |
| `CFO_OAUTH_REFRESH_TOKEN` | Rotating CFO.ai OAuth refresh token. | CFO.ai browser authorization; obtaining it is not documented in code. | Yes for allocation. |
| `CFO_SECRET_WRITE_TOKEN` | Separate GitHub token used to update the target repository's Actions secrets after CFO.ai rotates the refresh token. | GitHub personal access token with **Secrets: Read and write** permission in every repository sharing the login; token creation steps are not documented in code. | Yes for allocation. |

## Configuration

By default, each caller reads `.github/prism.json` from the target repository's default branch. To share one file, add `config: configs/portfolio.json` under `with:` in **both** caller workflows, immediately after `prisme_ref: main`. That file is read from Prisme at `prisme_ref`; the target repository needs only `.github/workflows/prism-estimate.yml` and `.github/workflows/prism-cfo-allocation.yml`, copied from `examples/`. The setup script accepts the same shared config path and then creates only these two callers. Leave `config` out to keep the per-repository behavior. Keep `prisme_ref` equal to the ref in each caller's `uses: ...@main` line.

[`configs/portfolio.json`](../configs/portfolio.json) sets OpenAI, `gpt-6-sol`, and the shared CFO login repositories. Replace every `PENDING` CFO.ai table value before allocation; a `PENDING` value stops the allocation before CFO.ai login. Put the same CFO OAuth client ID, current refresh token, and GitHub secret-write token in every listed repository. Run one allocation at a time across them.

Every key below is inside `prism.json`. “None” means the workflow or code requires a value rather than supplying a default.

| Key | What it does | Default | Example |
|---|---|---|---|
| `cfo.org_id` | CFO.ai organisation ID. | None | `"102909"` |
| `cfo.scenario` | Scenario holding the tables. | None | `"Main Scenario"` |
| `cfo.page` | Page holding decision and request tables. | None | `"K2 budgets"` |
| `cfo.budget_table` | Repository budget table name. | None | `"K2 repository budgets"` |
| `cfo.request_table` | Allocation request table name. | None | `"K2 request allocations"` |
| `cfo.decision_table` | Decision test table name, fed by the saved budget rule. | None | `"K2 decision test"` |
| `cfo.column_prefix` | Prefix for budget and request columns. | None | `"K2 "` |
| `cfo.shared_login_repos` | Repositories receiving each rotated refresh token; include the current repo and use the same list everywhere. | Current repo only | `["Bhavya031/k-2", "Bhavya031/flask"]` |
| `estimate.cooldown_minutes` | Delay between estimates in one repository. | `10` | `10` |
| `estimate.provider` | Provider for prediction and estimation. | None | `"openai"` |
| `estimate.worker_model` | Model whose future solve cost is estimated. | None | `"gpt-6-sol"` |
| `estimate.max_cost_usd` | Limit for prediction calls, in USD. | None | `0.05` |
| `estimate.max_output_tokens` | Output token limit for prediction calls. | None | `4000` |
| `estimate.max_extra_rounds` | Additional prediction rounds. | None | `1` |
| `estimate.timeout_seconds` | Prediction time limit. | None | `180` |

`gpt-6-sol` has built-in prices. A worker model absent from Prism's built-in price list makes estimation fail with “No price for worker model”; the workflow does not supply a separate pricing file or catalog.

The optional repository Actions variable `PRISM_REPLAY_REF` is a maintainer-chosen branch, tag, or commit for replaying an older source commit. Find or set it under Settings → Secrets and variables → Actions → Variables. Leave it unset for the default branch. The setup command does not ask for it because it is not needed for a new installation.

## CFO.ai page and tables

The configured decision and request tables must be on `cfo.page` in `cfo.scenario` for `cfo.org_id`; Prism checks their exact display names and complete reads. The budget rule saved in CFO.ai uses `K2 decision test` and `K2 decision rules` to produce an approved or blocked result, chosen model, allocated amount, and reason. Prism writes predicted-token inputs to the decision test, reads that result, sanity-checks it, and records a reservation only for a new approved request. The rule setup and its link to the budget table are not documented in code; the owner must confirm them in CFO.ai.

| CFO.ai item | Columns or value Prism expects | Where to get it | Required? |
|---|---|---|---|
| Page | Exact display name `cfo.page`, with the configured decision and request tables in `cfo.scenario`. | Create or select it in CFO.ai; exact navigation is not documented in code. | Yes. |
| Budget table `cfo.budget_table` | Example columns: `<prefix>repository` (GitHub `owner/repo`), `<prefix>budget USD`, `<prefix>spending USD`, `<prefix>existing reservations USD`, `<prefix>max budget fraction`. The rule's actual link and column requirements are not checked directly by Prism. | Confirm the budget row and its connection to the saved rule in CFO.ai; setup steps are not documented in code. | Required by the saved rule, not read directly by `cfo_actions.py`. |
| Decision table `cfo.decision_table` | The example uses `K2 decision test`. Row dimension `K2 decision test request`; Prism writes `K2 test repository`, `K2 test issue URL`, `K2 test uncached input tokens`, `K2 test cached input tokens`, `K2 test output tokens`. It reads `K2 test decision` (`approved` or `blocked`), `K2 test chosen model`, `K2 test allocated USD`, `K2 test reason`, `K2 test available USD`, `K2 test request limit USD`, `K2 test budget USD`, `K2 test spending USD`, `K2 test existing reservations USD`, `K2 test max budget fraction`, three `K2 test recommendation N` and `K2 test recommendation N cost USD` pairs (`N` = 1, 2, 3), and any `cost — MODEL (USD)` columns. | Use the decision test table backed by the saved rule; how to build its computed columns is not documented in code. | Yes. |
| Rule table `K2 decision rules` | The saved budget rule produces the decision test outputs above. Prism does not read this table directly or know its column schema. | Confirm it in CFO.ai; its creation and formulas are not documented in code. | Yes for the example rule. |
| Request table `cfo.request_table` | Dimension `<prefix>request ID`; then columns **in this exact order**: `<prefix>request repository`, `<prefix>issue URL`, `<prefix>pinned commit`, `<prefix>worker model`, `<prefix>predicted input tokens`, `<prefix>predicted uncached input tokens`, `<prefix>predicted cached input tokens`, `<prefix>predicted output tokens`, `<prefix>predicted total tokens`, `<prefix>estimated USD`, `<prefix>decision`, `<prefix>allocation ID`, `<prefix>allocated USD cap`, `<prefix>reservation USD`, `<prefix>available balance USD`, `<prefix>reason`. | Create an empty table in CFO.ai. Prism populates request rows; the code does not document a table creation UI. | Yes. |

Paste this into CFO.ai after replacing the placeholders with your `.github/prism.json` values and target GitHub repository:

> In organisation `<cfo.org_id>`, scenario `<cfo.scenario>`, use page `<cfo.page>`. Confirm that budget table `<cfo.budget_table>` has the `<owner/repo>` row and that the saved budget rule in `K2 decision rules` produces results in decision test table `<cfo.decision_table>` (the example name is `K2 decision test`). The decision test needs request dimension `K2 decision test request`; input columns `K2 test repository`, `K2 test issue URL`, `K2 test uncached input tokens`, `K2 test cached input tokens`, `K2 test output tokens`; and computed columns `K2 test decision`, `K2 test chosen model`, `K2 test allocated USD`, `K2 test reason`, `K2 test available USD`, `K2 test request limit USD`, `K2 test budget USD`, `K2 test spending USD`, `K2 test existing reservations USD`, `K2 test max budget fraction`, plus `K2 test recommendation 1`, `K2 test recommendation 1 cost USD` through recommendation 3 and optional `cost — MODEL (USD)` columns. Preserve those display names. Also create request table `<cfo.request_table>` with row dimension `<cfo.column_prefix>request ID` and these columns in exact order: `<cfo.column_prefix>request repository`, `<cfo.column_prefix>issue URL`, `<cfo.column_prefix>pinned commit`, `<cfo.column_prefix>worker model`, `<cfo.column_prefix>predicted input tokens`, `<cfo.column_prefix>predicted uncached input tokens`, `<cfo.column_prefix>predicted cached input tokens`, `<cfo.column_prefix>predicted output tokens`, `<cfo.column_prefix>predicted total tokens`, `<cfo.column_prefix>estimated USD`, `<cfo.column_prefix>decision`, `<cfo.column_prefix>allocation ID`, `<cfo.column_prefix>allocated USD cap`, `<cfo.column_prefix>reservation USD`, `<cfo.column_prefix>available balance USD`, `<cfo.column_prefix>reason`. Prism writes the inputs, checks the saved rule's result, and records approved reservations. Tell me if the existing CFO.ai rule or its table schema differs; its construction is not documented in code.

## CFO.ai sign-in and secret rotation

The CFO.ai refresh token rotates on every use. Before OAuth, each allocation workflow reads `.github/prism.json`, checks the Actions secret public key, and writes the harmless `CFO_SECRET_WRITE_PREFLIGHT` secret in every listed repository. The code also rewrites `CFO_SECRET_WRITE_TOKEN` with itself in each repository before refresh. Only after those writes succeed does it exchange `CFO_OAUTH_REFRESH_TOKEN`, save the replacement to every listed repository, and use the access token. If any save fails, allocation stops and names the repository; repositories may then hold different tokens and browser reauthorization may be required. The write token needs **Secrets: Read and write** permission in every listed repository. Run only one allocation at a time across repositories sharing a login: the `prism-cfo-allocation` concurrency group serializes runs inside one repository, not across repositories.

You can run the manual **Prism automated estimation and allocation validation** workflow with `preflight_only` to check secret-write access without contacting CFO.ai. A normal estimate will call the configured model provider and CFO.ai.

## Paste this into your coding agent

```text
Set up Prism automated estimation and allocation on my GitHub repository interactively. Read Prisme's docs/SETUP.md, examples/prism-estimate.yml, examples/prism-cfo-allocation.yml, examples/prism.json, src/prism/cfo_actions.py, and src/prism/bot.py first. Ask me for the target repository, whether to use a shared Prisme config, and each required .github/prism.json field when configuring locally and each secret listed in docs/SETUP.md; ask whether I want the optional NEATLOGS_API_KEY and shared-login repository list. Do not ask me to paste secret values into chat or display them: have me enter them hidden in a terminal. Use bash scripts/setup-github.sh /path/to/my/repo from a Prisme checkout, or perform its same steps with gh CLI: set the target repository Actions secrets from hidden stdin, write .github/prism.json only for local config, and copy the two small example caller workflows with a matching `config` input for shared config. Ask me to create the CFO.ai page and tables using the prompt in docs/SETUP.md and to make my Entire mirror ready. Check the generated config and workflow destinations, then tell me which files to commit and push to the target repository's default branch. Do not run a workflow or call a paid model without my request.
```
