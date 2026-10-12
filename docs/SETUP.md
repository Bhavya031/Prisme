# Set up Prism on a GitHub repository

From a Prisme checkout, run `prism init --repo owner/name` (or `prism init --repo owner/name --config configs/portfolio.json` for the shared config). You need `gh` authenticated for that repository and permission to set its Actions secrets. The command asks for missing settings, hides secret input, and offers to add `.github/prism.json` and the two small caller files in `examples/` to a local target checkout. Review and commit those files on the target repository's default branch. The older `bash scripts/setup-github.sh /path/to/your/repo` remains available. Prisme must be public before public forks can call its reusable workflows.

Before opening an issue, register and index the same `owner/repo` in Entire. [The existing install guide](install-github-estimate.md#entire-setup-and-current-credential-limit) gives the available CLI commands. The workflow requires `/gh/owner/repo` to report `ready` before it makes a model call. Set repository Actions permissions to allow the workflow's `issues: write`, `contents: read`, and `actions: read` declarations. An owner, member, or collaborator can open an issue or comment exactly `/prism estimate` to start an estimate; a successful estimate starts allocation.

## Check credentials before a run

From the directory containing `.env`, run `prism check --repo owner/name` for the default `.github/prism.json`, or add `--config configs/portfolio.json` for the shared config. The command uses the same `.env` loading as other Prism commands. Its overview separates blockers from unverified and optional items, then lists only the actions needed to clear blockers. It checks Entire readiness, the configured provider key with a free model-list request, the built-in worker-model price, CFO config and GitHub secret metadata, read access to every shared repository's Actions secrets public key, and the Glance binary. A blocker gives a nonzero exit. No model completion, CFO.ai request, or secret write is made.

For a computer-only setup, `prism init` marks `.env` for local credentials, and `prism check` uses those credentials without checking Actions secrets. Pass `--repo owner/name` to verify Entire's repository readiness; without a repository, that check remains a blocker. A local CFO.ai allocation saves each rotated refresh token back to `.env`.

The CFO.ai refresh token cannot be validated safely without rotating it. The check looks for the client ID and refresh token in GitHub secret metadata when its read token permits this; a missing local refresh token alone is not a blocker. Reading GitHub's public key proves read access, not permission to write secrets. Glance is shown as optional when absent; install it before a local estimate that needs context measurement.

Run `prism init` for guided setup, or choose the guide-only option on its first screen. Add `--repo owner/name` and `--config configs/portfolio.json` when useful. Choose GitHub repository, this computer, or both. GitHub setup asks for a repository link and checks `gh` login, repository access, admin or maintain permission, and push permission before changing setup. In a terminal, use ↑/↓ and Enter or a number key to choose; Esc or q cancels. Nonterminal input uses numbered prompts. GitHub setup asks for a local checkout when it needs to create `.github/prism.json` or caller files. Local setup saves credentials in a private `.env` in the current folder and warns when Git does not ignore it. For both destinations, CFO.ai needs two separate browser sign-ins because each refresh token rotates on use. Review and commit any config or caller files it adds; the command does not commit or push them. The older `scripts/setup-github.sh` remains available, but `prism init` is the guided entry point.

## How to get Entire

Create an Entire account and connect the GitHub repository to it. Run `entire login` in the region containing your mirror. Check `entire repo mirror list --available --name owner/repo`, then add it with `entire repo mirror add /gh/owner/repo`. Wait until `entire repo view /gh/owner/repo --json` reports `ready`. Run `entire auth token` to obtain the current login-session token, then save it as the repository's `ENTIRE_TOKEN` Actions secret with `prism init`. This is a short-lived login session, observed to expire within a day; refresh the secret before a run if it expires.

## How to get a model key

Create an API key with the provider selected by `estimate.provider` (OpenAI or Anthropic). Use `prism init` to enter it hidden and save only that provider's key to the target repository. `prism check` uses a free model-list request to check the key; it does not make a completion call.

## How to get CFO.ai

Create a CFO.ai account and set up the page, budget table, decision table, request table, and saved budget rule described below. Enter their exact names under `cfo` in the Prism config. Run `prism cfo-login --repo owner/name` and approve the browser sign-in; repeat `--repo` for every repository sharing one login. `prism init` can offer this sign-in for you. The command saves the public client ID and rotating refresh token as Actions secrets. A real allocation run is the only safe validation of the refresh token because checking it would rotate it.

## How to get a GitHub token

Create a GitHub personal access token with **Secrets: Read and write** permission for every repository in `cfo.shared_login_repos`. Save it as `CFO_SECRET_WRITE_TOKEN` with `prism init`. `prism check` reads each repository's Actions secrets public key to confirm reachability; that read cannot prove write permission.

## Values to provide

These secrets are passed explicitly by the caller workflows. Set them in the **target** repository's Settings → Secrets and variables → Actions → Repository secrets. The setup command asks for them without showing their values. `GITHUB_TOKEN` is supplied by Actions and uses the workflow permissions above; it is not a secret you provide. The preflight creates `CFO_SECRET_WRITE_PREFLIGHT` with the fixed value `ok`; you do not provide it.

| Secret | What it is | Where to get it | Required? |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Key for Claude prediction and estimation. | Anthropic API account. | Only when `estimate.provider` is `claude`. |
| `OPENAI_API_KEY` | Key for OpenAI prediction and estimation. | OpenAI API account. | Only when `estimate.provider` is `openai`. |
| `NEATLOGS_API_KEY` | Sends prediction traces to Neatlogs. | Neatlogs account; issuance steps are not documented in code. | No. Press Enter to skip. |
| `ENTIRE_TOKEN` | Entire credential for searching the indexed target repository. | Log in to Entire in the region containing the mirror and create a token there; the exact issuance flow is not documented in code. | Yes. |
| `PRISME_READ_TOKEN` | GitHub token that can read private `Bhavya031/Prisme`, used by install and checkout. | GitHub personal access token with Prisme Contents read permission. | Only while Prisme is private. |
| `CFO_OAUTH_CLIENT_ID` | CFO.ai OAuth client ID used with the refresh token. | `prism cfo-login` below. | Yes for allocation. |
| `CFO_OAUTH_REFRESH_TOKEN` | Rotating CFO.ai OAuth refresh token. | `prism cfo-login` below. | Yes for allocation. |
| `CFO_SECRET_WRITE_TOKEN` | Separate GitHub token used to update the target repository's Actions secrets after CFO.ai rotates the refresh token. | GitHub personal access token with **Secrets: Read and write** permission in every repository sharing the login; token creation steps are not documented in code. | Yes for allocation. |

## Configuration

By default, each caller reads `.github/prism.json` from the target repository's default branch. To share one file, add `config: configs/portfolio.json` under `with:` in **both** caller workflows, immediately after `prisme_ref: main`. That file is read from Prisme at `prisme_ref`; the target repository needs only `.github/workflows/prism-estimate.yml` and `.github/workflows/prism-cfo-allocation.yml`, copied from `examples/`. The setup script accepts the same shared config path and then creates only these two callers. Leave `config` out to keep the per-repository behavior. Keep `prisme_ref` equal to the ref in each caller's `uses: ...@main` line.

[`configs/portfolio.json`](../configs/portfolio.json) sets OpenAI, `gpt-6-sol`, and the shared CFO login repositories. The portfolio file now names the Engineering portfolio page and tables; confirm them in CFO.ai before allocation. Put the same CFO OAuth client ID, current refresh token, and GitHub secret-write token in every listed repository. Run one allocation at a time across them.

Every key below is inside `prism.json`. “None” means the workflow or code requires a value rather than supplying a default.

| Key | What it does | Default | Example |
|---|---|---|---|
| `cfo.org_id` | CFO.ai organisation ID. | None | `"102909"` |
| `cfo.scenario` | Scenario holding the tables. | None | `"Main Scenario"` |
| `cfo.page` | Page holding decision and request tables. | None | `"K2 budgets"` |
| `cfo.budget_table` | Repository budget table name. | None | `"K2 repository budgets"` |
| `cfo.request_table` | Allocation request table name. | None | `"K2 request allocations"` |
| `cfo.decision_table` | Decision test table name, fed by the saved budget rule. | None | `"K2 decision test"` |
| `cfo.column_prefix` | Prefix for decision and request columns. | None | `"K2 "` |
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

Prism reads the configured decision and request tables on `cfo.page` in `cfo.scenario` for `cfo.org_id`. It does **not** read `cfo.budget_table` directly: the saved CFO.ai budget rule must supply budget and reservation results in the decision row. The portfolio config names page `Engineering portfolio`, budget table `Portfolio repository budgets`, decision table `Portfolio decision test`, and request table `Portfolio request allocations`. Prism does not know the saved rule's table name or how CFO.ai links it to the budget table.

`<prefix>` below means the exact value of `cfo.column_prefix`: `K2 ` for k-2 or `Portfolio ` for the shared portfolio. The decision table uses row dimension `<prefix>decision test request`. Prism writes `<prefix>test repository`, `<prefix>test issue URL`, `<prefix>test uncached input tokens`, `<prefix>test cached input tokens`, and `<prefix>test output tokens`. The saved rule must supply `<prefix>test decision` (`approved` or `blocked`), `<prefix>test chosen model`, `<prefix>test allocated USD`, `<prefix>test reason`, `<prefix>test available USD`, `<prefix>test request limit USD`, `<prefix>test budget USD`, `<prefix>test spending USD`, `<prefix>test existing reservations USD`, and `<prefix>test max budget fraction`. It also reads three `<prefix>test recommendation N` and `<prefix>test recommendation N cost USD` pairs, with N from 1 to 3. Optional per-model costs may be named `cost — MODEL (USD)` or `<prefix>cost — MODEL (USD)`; empty and non-USD columns are ignored. When the request table has an allocation month column, the decision row must also supply `<prefix>test allocation month`.

The request table uses row dimension `<prefix>request ID`, followed by these columns **in this order**: `<prefix>request repository`, `<prefix>issue URL`, `<prefix>pinned commit`, `<prefix>worker model`, `<prefix>predicted input tokens`, `<prefix>predicted uncached input tokens`, `<prefix>predicted cached input tokens`, `<prefix>predicted output tokens`, `<prefix>predicted total tokens`, `<prefix>estimated USD`, `<prefix>decision`, `<prefix>allocation ID`, `<prefix>allocated USD cap`, `<prefix>reservation USD`, `<prefix>available balance USD`, and `<prefix>reason`. It may have one final `<prefix>allocation month` column; Prism requires text from the decision row, copies it unchanged, and checks the read-back. The user confirmed that `Portfolio test allocation month` shows `2026-10-01` for a test row and that the reservation sum counts a request row when `Portfolio allocation month` is `2026-10-01`, `Portfolio decision` is exactly `allocated`, and `Portfolio request repository` matches. Whether MCP returns that month as text has not been checked. If CFO.ai returns a `Formulas` entry as a column, the exact-shape check will reject the table; the K2 code did not handle such an entry, and its MCP representation has not been verified.

For the portfolio budget table, the supplied schema is row dimension `Portfolio budget record` with columns `Portfolio budget repository`, `Portfolio budget USD`, `Portfolio spending USD`, `Portfolio existing reservations USD`, and `Portfolio max budget fraction`. These names and the saved rule's connection to them must be confirmed in CFO.ai because Prism does not inspect that table. The old K2 budget row dimension and columns cannot be confirmed from code; only its decision and request names were encoded there.

## CFO.ai sign-in and secret rotation

For the first sign-in, run `prism cfo-login --repo owner/name` (repeat `--repo` for every repository sharing the login), then approve once in your browser while signed in to app.cfo.ai. The command checks Actions secret writes first, registers a public OAuth client, and saves `CFO_OAUTH_CLIENT_ID` and `CFO_OAUTH_REFRESH_TOKEN` as Actions secrets in each named repository. It prints the public client ID and sign-in link, never the refresh token. For an existing public client, use `--client-id ID --port PORT` with its registered loopback port. The callback wait defaults to five minutes; change it with `--timeout SECONDS`. Prism renews and saves the rotating refresh token on every allocation run after that.

The CFO.ai refresh token rotates on every use. Before OAuth, each allocation workflow reads its selected Prism config, checks the Actions secret public key, and writes the harmless `CFO_SECRET_WRITE_PREFLIGHT` secret in every listed repository. The code also rewrites `CFO_SECRET_WRITE_TOKEN` with itself in each repository before refresh. Only after those writes succeed does it exchange `CFO_OAUTH_REFRESH_TOKEN`, save the replacement to every listed repository, and use the access token. If any save fails, allocation stops and names the repository; repositories may then hold different tokens and browser reauthorization may be required. The write token needs **Secrets: Read and write** permission in every listed repository. Run only one allocation at a time across repositories sharing a login: the `prism-cfo-allocation` concurrency group serializes runs inside one repository, not across repositories.

You can run the manual **Prism automated estimation and allocation validation** workflow with `preflight_only` to check secret-write access without contacting CFO.ai. A normal estimate will call the configured model provider and CFO.ai.

## Paste this into your coding agent

```text
Set up Prism automated estimation and allocation on my GitHub repository interactively. Read Prisme's docs/SETUP.md, examples/prism-estimate.yml, examples/prism-cfo-allocation.yml, examples/prism.json, src/prism/cfo_actions.py, and src/prism/bot.py first. Ask me for the target repository, whether to use a shared Prisme config, and each required .github/prism.json field when configuring locally and each secret listed in docs/SETUP.md; ask whether I want the optional NEATLOGS_API_KEY and shared-login repository list. Do not ask me to paste secret values into chat or display them: have me enter them hidden in a terminal. Use bash scripts/setup-github.sh /path/to/my/repo from a Prisme checkout, or perform its same steps with gh CLI: set the target repository Actions secrets from hidden stdin, write .github/prism.json only for local config, and copy the two small example caller workflows with a matching `config` input for shared config. Ask me to create the CFO.ai page and tables using the prompt in docs/SETUP.md and to make my Entire mirror ready. Check the generated config and workflow destinations, then tell me which files to commit and push to the target repository's default branch. Do not run a workflow or call a paid model without my request.
```
