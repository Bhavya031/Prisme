# Set up Prism on a GitHub repository

From a Prisme checkout, run `bash scripts/setup-github.sh /path/to/your/repo`. You need `gh` authenticated for that repository and permission to set its Actions secrets. The command asks for every setting, hides secret input, writes `.github/prism.json`, and copies the two files in `examples/` to `.github/workflows/`. Review and commit those three files on the target repository's default branch. It stops if any destination file already exists.

Before opening an issue, register and index the same `owner/repo` in Entire. [The existing install guide](install-github-estimate.md#entire-setup-and-current-credential-limit) gives the available CLI commands. The workflow requires `/gh/owner/repo` to report `ready` before it makes a model call. Set repository Actions permissions to allow the workflow's `issues: write`, `contents: read`, and `actions: read` declarations. An owner, member, or collaborator can open an issue or comment exactly `/prism estimate` to start an estimate; a successful estimate starts allocation.

## Values to provide

These are the secrets read by either example workflow. Set them in the **target** repository's Settings → Secrets and variables → Actions → Repository secrets. The setup command asks for them without showing their values. `GITHUB_TOKEN` is supplied by Actions and uses the workflow permissions above; it is not a secret you provide. The preflight creates `CFO_SECRET_WRITE_PREFLIGHT` with the fixed value `ok`; you do not provide it.

| Secret | What it is | Where to get it | Required? |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Key for the example's Claude prediction and estimate calls. | Anthropic API account; issuance steps are not documented in code. | Yes for the example provider. |
| `NEATLOGS_API_KEY` | Sends prediction traces to Neatlogs. | Neatlogs account; issuance steps are not documented in code. | No. Press Enter to skip. |
| `ENTIRE_TOKEN` | Entire credential for searching the indexed target repository. | Log in to Entire in the region containing the mirror and create a token there; the exact issuance flow is not documented in code. | Yes. |
| `PRISME_READ_TOKEN` | GitHub token that can read the private `Bhavya031/Prisme` repository, used by install and checkout. | GitHub personal access token with access to `Bhavya031/Prisme` and repository Contents read permission; token creation steps are not documented in code. | Yes while that repository is private. |
| `CFO_OAUTH_CLIENT_ID` | CFO.ai OAuth client ID used with the refresh token. | CFO.ai OAuth registration; obtaining it is not documented in code. | Yes for allocation. |
| `CFO_OAUTH_REFRESH_TOKEN` | Rotating CFO.ai OAuth refresh token. | CFO.ai browser authorization; obtaining it is not documented in code. | Yes for allocation. |
| `CFO_SECRET_WRITE_TOKEN` | Separate GitHub token used to update the target repository's Actions secrets after CFO.ai rotates the refresh token. | GitHub personal access token with access to the **target** repository and repository **Secrets: Read and write** permission; token creation steps are not documented in code. | Yes for allocation. |

`.github/prism.json` follows [`examples/prism.json`](../examples/prism.json). The setup command asks for every field; it offers the example estimate values and scenario name as defaults. Do not use the example CFO organisation or table names unless they exist in your CFO.ai workspace.

| Field | What it is | Where to get it | Required? |
|---|---|---|---|
| `cfo.org_id` | Organisation passed to CFO.ai model tools. | Your CFO.ai organisation; how to find its ID is not documented in code. | Yes, nonempty string. |
| `cfo.scenario` | Scenario containing the tables. | Choose or create it in CFO.ai; exact navigation is not documented in code. | Yes, nonempty string. |
| `cfo.page` | Page that must contain both tables. | Choose or create it in the CFO.ai scenario; exact navigation is not documented in code. | Yes, nonempty string. |
| `cfo.budget_table` | Exact display name of the repository budget table. | Name it when creating the table on `cfo.page`. | Yes, nonempty string. |
| `cfo.request_table` | Exact display name of the allocation request table. | Name it when creating the table on `cfo.page`. | Yes, nonempty string. |
| `cfo.column_prefix` | Prefix on every column name below, including the request ID dimension. | Choose it when creating both tables; an empty prefix is rejected by the code. | Yes, nonempty string. |
| `estimate.cooldown_minutes` | Repository-wide delay between Prism estimates. | Choose a nonnegative number of minutes; example: `10`. | Yes; workflow falls back to `10` if absent. |
| `estimate.provider` | Model provider for file prediction and estimation. | Choose a provider supported by Prism; example: `claude`. | Yes. |
| `estimate.worker_model` | Model whose future solve cost is estimated. | Choose the worker model you plan to use; example: `claude-sonnet-5-5`. | Yes. |
| `estimate.max_cost_usd` | Maximum cost for file prediction calls. | Choose a USD limit; example: `0.05`. | Yes. |
| `estimate.max_output_tokens` | Prediction output token limit. | Choose a count; example: `4000`. | Yes. |
| `estimate.max_extra_rounds` | Additional prediction rounds allowed. | Choose a count; example: `1`. | Yes. |
| `estimate.timeout_seconds` | Prediction time limit. | Choose a duration; example: `180`. | Yes. |

The optional repository Actions variable `PRISM_REPLAY_REF` is a maintainer-chosen branch, tag, or commit for replaying an older source commit. Find or set it under Settings → Secrets and variables → Actions → Variables. Leave it unset for the default branch. The setup command does not ask for it because it is not needed for a new installation.

## CFO.ai page and tables

Both tables must be on `cfo.page` in `cfo.scenario` for `cfo.org_id`. Prism checks their exact display names and complete reads. Create exactly one budget row whose **`<cfo.column_prefix>repository`** equals the target GitHub `owner/repo`. The repository name comes from the target repository; the budget and policy amounts are values its owner chooses. Prism code applies the budget policy through CFO.ai model tools and writes the allocation request row.

| CFO.ai item | Columns or value Prism expects | Where to get it | Required? |
|---|---|---|---|
| Page | Exact display name `cfo.page`, with both tables in `cfo.scenario`. | Create or select it in CFO.ai; exact navigation is not documented in code. | Yes. |
| Budget table `cfo.budget_table` | `<prefix>repository` (GitHub `owner/repo`), `<prefix>budget USD`, `<prefix>spending USD`, `<prefix>existing reservations USD`, `<prefix>max budget fraction`. The last four are numbers; the fraction must be above 0 and at most 1. | Create the table in CFO.ai; choose budget, spending, opening reservations, and policy fraction. The code does not document a table creation UI. | Yes, including one matching row. |
| Request table `cfo.request_table` | Dimension `<prefix>request ID`; then columns **in this exact order**: `<prefix>request repository`, `<prefix>issue URL`, `<prefix>pinned commit`, `<prefix>worker model`, `<prefix>predicted input tokens`, `<prefix>predicted uncached input tokens`, `<prefix>predicted cached input tokens`, `<prefix>predicted output tokens`, `<prefix>predicted total tokens`, `<prefix>estimated USD`, `<prefix>decision`, `<prefix>allocation ID`, `<prefix>allocated USD cap`, `<prefix>reservation USD`, `<prefix>available balance USD`, `<prefix>reason`. | Create an empty table in CFO.ai. Prism populates request rows; the code does not document a table creation UI. | Yes. |

Paste this into CFO.ai after replacing the placeholders with your `.github/prism.json` values and target GitHub repository:

> In organisation `<cfo.org_id>`, scenario `<cfo.scenario>`, create page `<cfo.page>`. On that page create table `<cfo.budget_table>` with columns `<cfo.column_prefix>repository`, `<cfo.column_prefix>budget USD`, `<cfo.column_prefix>spending USD`, `<cfo.column_prefix>existing reservations USD`, and `<cfo.column_prefix>max budget fraction`. Add exactly one row for `<owner/repo>` with budget USD `<budget>`, spending USD `<spent>`, existing reservations USD `<opening reservations>`, and max budget fraction `<fraction from 0 to 1>`. Also create an empty table `<cfo.request_table>` with row dimension `<cfo.column_prefix>request ID` and these columns in this exact order: `<cfo.column_prefix>request repository`, `<cfo.column_prefix>issue URL`, `<cfo.column_prefix>pinned commit`, `<cfo.column_prefix>worker model`, `<cfo.column_prefix>predicted input tokens`, `<cfo.column_prefix>predicted uncached input tokens`, `<cfo.column_prefix>predicted cached input tokens`, `<cfo.column_prefix>predicted output tokens`, `<cfo.column_prefix>predicted total tokens`, `<cfo.column_prefix>estimated USD`, `<cfo.column_prefix>decision`, `<cfo.column_prefix>allocation ID`, `<cfo.column_prefix>allocated USD cap`, `<cfo.column_prefix>reservation USD`, `<cfo.column_prefix>available balance USD`, `<cfo.column_prefix>reason`. Preserve these display names exactly. Prism code applies the budget policy; do not add a separate decision rule.

## CFO.ai sign-in and secret rotation

The CFO.ai refresh token rotates on every use. Before OAuth, each allocation workflow tests the separate `CFO_SECRET_WRITE_TOKEN` by reading the target repository's Actions secret public key and writing the harmless `CFO_SECRET_WRITE_PREFLIGHT` secret. The code also rewrites `CFO_SECRET_WRITE_TOKEN` with itself as a preflight. Only after those writes succeed does it exchange `CFO_OAUTH_REFRESH_TOKEN`, save the replacement back to that repository secret, and use the access token. A failed save stops allocation and may require browser reauthorization. The write token needs repository **Secrets: Read and write** permission; the built-in `GITHUB_TOKEN` is insufficient for this operation. The example workflows share the `prism-cfo-allocation` concurrency group, so two allocation runs must never overlap: either run could otherwise use an invalidated refresh token or race to save the replacement.

You can run the manual **Prism automated estimation and allocation validation** workflow with `preflight_only` to check secret-write access without contacting CFO.ai. A normal estimate will call the configured model provider and CFO.ai.

## Paste this into your coding agent

```text
Set up Prism automated estimation and allocation on my GitHub repository interactively. Read Prisme's docs/SETUP.md, examples/prism-estimate.yml, examples/prism-cfo-allocation.yml, examples/prism.json, src/prism/cfo_actions.py, and src/prism/bot.py first. Ask me for the target repository and each required .github/prism.json field and each secret listed in docs/SETUP.md; ask whether I want the optional NEATLOGS_API_KEY. Do not ask me to paste secret values into chat or display them: have me enter them hidden in a terminal. Use bash scripts/setup-github.sh /path/to/my/repo from a Prisme checkout, or perform its same steps with gh CLI: set the target repository Actions secrets from hidden stdin, write .github/prism.json, and copy the two example workflows unchanged. Ask me to create the CFO.ai page and tables using the prompt in docs/SETUP.md and to make my Entire mirror ready. Check the generated config and workflow destinations, then tell me which files to commit and push to the target repository's default branch. Do not run a workflow or call a paid model without my request.
```
