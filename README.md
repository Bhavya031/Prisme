# Prism

A prism splits one beam into its parts. **Prism** takes one GitHub issue and shows three sides before a coding worker starts: **scope** (the existing files likely to change), **usage** (predicted tokens), and **cost** (a USD estimate followed by a budget-rule allocation). It provides automated estimation and allocation; it does not implement the issue.

## What it is

Prism is a Python CLI with example GitHub Actions workflows. An eligible issue or `/prism estimate` comment starts a file prediction, a heuristic estimate of a future coding run, and then an allocation check using the budget rule saved in CFO.ai. Scope and usage are predictions, not a plan or a bill. The estimate labels its assumptions as unvalidated.

## How it works

```mermaid
flowchart TD
    A[Issue opened or /prism estimate comment] --> B[Acknowledge with eyes reaction]
    A --> C[Pin one source commit]
    C --> D[Check authorization, duplicates, and cooldown]
    D --> E[Search repository and predict existing files: SCOPE]
    E --> F[Count selected context and project run tokens: USAGE]
    F --> G[Price token estimates in USD: COST]
    G --> H[Post estimate comment on issue]
    H --> I[Write or reuse predicted-token inputs in CFO.ai decision test]
    I --> J[Read saved budget rule result]
    J --> K[Prism sanity-checks decision, model, amount, reason]
    K --> L[Check request ID against existing reservations]
    L -->|new approved request| M[Write reservation row through CFO.ai model tools]
    M --> N[Post allocation table, models, and requester mention]
    L -->|blocked or already recorded| N
```

1. The example workflow reacts to new issues and exact `/prism estimate` comments from repository owners, members, or collaborators. A separate job adds 👀. The estimate job pins the default branch commit (or a maintainer-set replay ref), then checks duplicate requests and a repository-wide cooldown before model calls.
2. **Scope:** a model searches the ready Entire mirror for a live issue and predicts existing file paths. A replay searches Git at the pinned commit. Prism reads selected file contents from that commit.
3. **Usage:** Glance counts the issue text and selected file contents as a starting context. Prism applies explicit, unvalidated assumptions to project low, central, and high token usage for a future worker run.
4. **Cost:** Prism prices those projections with model list prices and adds the file-discovery call's known cost. It posts the estimate first. On success, it writes predicted-token inputs to CFO.ai's decision test table, reads the saved budget rule's result, and sanity-checks the allow/block decision, chosen model, allocated amount, and reason. It writes a reservation for a new approved request and posts a separate result. If that request ID already has a reservation row, it uses the existing record without a second reservation.

## Integrations

| Integration | Role today |
|---|---|
| [Entire](https://entire.io/) | Provides the searchable mirror that names candidate files for live issues; Prism reads their contents from the pinned Git commit. |
| [Anthropic](https://www.anthropic.com/) and [OpenAI](https://openai.com/) | Claude or OpenAI predicts likely files. Select one with `estimate.provider`; the shared portfolio config selects OpenAI. |
| [Glance](https://github.com/Bhavya031/Glance) | Counts the exact starting context bytes that Prism assembles; it does not pick files or estimate the whole worker run. |
| [Neatlogs](https://neatlogs.com/) | Optional tracing for Prism's model workflow when `NEATLOGS_API_KEY` is set. It does not determine scope, usage, cost, or allocation. |
| [CFO.ai](https://cfo.ai/) | Holds the saved budget rule and its decision test, budget, and request tables. Prism reads the rule's result, checks it, and records approved reservations through CFO.ai model tools. |

## Install

From a Prisme checkout, install the CLI (`pip install -e .`), then run `prism init --repo owner/name`. Choose **This computer**, **GitHub repository**, or **Both**. The guide walks through credentials, config, and caller files; review and commit any files it adds to the target repository. See [SETUP.md](docs/SETUP.md) for prerequisites and the manual setup steps.

Run `prism check --repo owner/name` to see the setup-check table before a run. It makes no model completion, CFO.ai request, or secret write.

| Setup-check row | Checks |
|---|---|
| Config; OpenAI or Anthropic | Settings, provider key, and worker-model price |
| Entire | Token and repository mirror readiness |
| CFO.ai; GitHub | Table settings, sign-in metadata, and shared repository secret access |
| NeatLogs; Glance | Optional tracing key and local token counter |

`BLOCK` needs action; `WARN` means Prism could not fully verify it; `MISSING` can mean an optional item is absent. For the first CFO.ai sign-in, run `prism cfo-login --repo owner/name` and approve in the browser. It saves the client ID and rotating refresh token as Actions secrets. For computer-only setup, `prism init` saves local credentials in `.env`.

For GitHub Actions, copy [`examples/prism-estimate.yml`](examples/prism-estimate.yml) and [`examples/prism-cfo-allocation.yml`](examples/prism-cfo-allocation.yml) into the target repository's `.github/workflows/`. They call the shared reusable workflows in Prisme. Prisme must be public for public forks to call them.

## Configuration

The workflows read `.github/prism.json` from the target repository's default branch unless both callers set `config: configs/portfolio.json` under `with:`. That shared file in Prisme selects OpenAI and `gpt-6-sol` and names the repositories sharing CFO.ai login. The [`examples/prism.json`](examples/prism.json) shape has two groups:

| Group | Keys |
|---|---|
| `estimate` | `cooldown_minutes`, `provider`, `worker_model`, `max_cost_usd`, `max_output_tokens`, `max_extra_rounds`, `timeout_seconds` control the estimate and its file-discovery limits. |
| `cfo` | `org_id`, `scenario`, `page`, `budget_table`, `request_table`, `decision_table`, `column_prefix` name the CFO.ai tables and columns used for the rule result and reservation. Optional `shared_login_repos` lists every `owner/repo` sharing one rotating login. |

See [SETUP.md](docs/SETUP.md) for each field, secret, and required column. The optional Actions variable `PRISM_REPLAY_REF` pins a historical branch, tag, or commit; without it, the workflow uses the repository default branch.

## What appears on the issue

- **Estimate comment:** one marked comment is first reserved as “Estimating,” then updated with predicted files (**scope**), measured starting context and low/central/high projected tokens (**usage**), solve and end-to-end USD estimates (**cost**), discovery spend, worker model, and pinned commit. A failure updates it with the stopping reason when possible.
- **Allocation comment:** on a successful estimate, a separate comment mentions the requester, repeats the predicted files, and shows the budget, spending, reservations, allocated amount, remaining balance, and request limit. It shows the saved budget rule's chosen model and reason, three suggested models, and other models in a fold-out. A blocked result has no new reservation. When the request ID already has a reservation row, the comment says it was already recorded and no second reservation was added.

## Results

In a benchmark of 15 real open-source issues, Claude Sonnet found 73% of fix files on average (71% precision; 5 of 15 exact file sets). OpenAI GPT-6 Sol found 83% (62% precision; 3 of 15 exact). Estimated file-discovery cost was about $0.71 total for 60 model calls. This run used direct Git search at each pinned pre-merge commit, not Entire search. It measures file prediction only, not cost-estimate accuracy or completed fixes.

## CFO.ai data boundary

Prism writes the repository name, issue URL, and projected **uncached input, cached input, and output tokens** to the configured decision test table. The saved budget rule supplies the allow/block decision, chosen model, allocated USD amount, reason, and model suggestions. Prism reads the computed decision row, checks the inputs and approved amount against available funds and the request limit, then writes the pinned commit, chosen model, token counts, amount, and reason to the reservation table for a new approved request. It reads the reservation back to confirm it. **Prism prices its own estimate before this step; it sends no price list, repository code, or issue text in these CFO.ai calls.** The issue URL is included.

## Limits and not built yet

- The low/central/high usage and cost projections use assumptions that have not been calibrated against a real coding-worker run. Model API charges for discovery can occur before a cost limit stops further calls.
- Scope predicts existing files only. A live issue needs a ready Entire mirror; a missing or failed index stops the run. Selected context excludes files that are missing or not text at the pinned commit.
- An observed Entire login token expired after eight hours. Refresh `ENTIRE_TOKEN` when it expires; no long-lived CI credential is set up here.
- CFO.ai pages, tables, and the saved budget rule must be set up by hand before allocation.
- The example workflows estimate and reserve budget; they do not launch a solve worker. **Actual-spend report-back and reconciliation are not built into this workflow yet.**
- CFO.ai refresh tokens rotate. Put the same `CFO_OAUTH_CLIENT_ID`, `CFO_OAUTH_REFRESH_TOKEN`, and `CFO_SECRET_WRITE_TOKEN` in each repository sharing a login, and list all repositories in each `.github/prism.json`. Run only one allocation at a time across them; workflow concurrency applies within one repository only.

## Sponsors

Thanks to the projects whose tools make the scope, usage, and cost workflow possible:

| | |
|---|---|
| <img src="https://raw.githubusercontent.com/Bhavya031/Glance/main/assets/logo.png" alt="Glance logo" width="48"> | [Glance](https://github.com/Bhavya031/Glance) |
| <img src="https://avatars.githubusercontent.com/u/33188652?v=4" alt="Entire logo" width="48"> | [Entire](https://entire.io/) |
| <img src="https://neatlogs.com/apple-icon.png" alt="Neatlogs logo" width="48"> | [Neatlogs](https://neatlogs.com/) |
| <img src="https://cfo.ai/apple-icon.png" alt="CFO.ai logo" width="48"> | [CFO.ai](https://cfo.ai/) |
| <img src="https://avatars.githubusercontent.com/u/76263028?v=4" alt="Anthropic logo" width="48"> | [Anthropic](https://www.anthropic.com/) |
