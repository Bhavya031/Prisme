# Prism

A prism splits one beam into its parts. **Prism** takes one GitHub issue and shows three sides before a coding worker starts: **scope** (the existing files likely to change), **usage** (predicted tokens), and **cost** (a USD estimate followed by a budget allocation). It provides automated estimation and allocation; it does not implement the issue.

## What it is

Prism is a Python CLI with example GitHub Actions workflows. An eligible issue or `/prism estimate` comment starts a file prediction, a heuristic estimate of a future coding run, and then an allocation check against repository budget tables. Scope and usage are predictions, not a plan or a bill. The estimate labels its assumptions as unvalidated.

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
    H --> I[Read budget and request rows from CFO.ai]
    I --> J[Prism code applies budget policy]
    J -->|allocated| K[Write reservation row through CFO.ai model tools]
    K --> L[Post allocation table and requester mention on issue]
    J -->|blocked or denied| L
```

1. The example workflow reacts to new issues and exact `/prism estimate` comments from repository owners, members, or collaborators. A separate job adds 👀. The estimate job pins the default branch commit (or a maintainer-set replay ref), then checks duplicate requests and a repository-wide cooldown before model calls.
2. **Scope:** a model searches the ready Entire mirror for a live issue and predicts existing file paths. A replay searches Git at the pinned commit. Prism reads selected file contents from that commit.
3. **Usage:** Glance counts the issue text and selected file contents as a starting context. Prism applies explicit, unvalidated assumptions to project low, central, and high token usage for a future worker run.
4. **Cost:** Prism prices those projections with model list prices and adds the file-discovery call's known cost. It posts the estimate first. On success, Prism reads the CFO.ai tables, applies the repository's budget rule in its own code, writes a reservation row when allocated, and posts a separate result.

## Integrations

| Integration | Role today |
|---|---|
| [Entire](https://entire.io/) | Provides the searchable mirror that names candidate files for live issues; Prism reads their contents from the pinned Git commit. |
| [Anthropic](https://www.anthropic.com/) | The example's Claude provider predicts likely files from issue and repository context. Prism also supports an OpenAI provider, but the example config selects Claude. |
| [Glance](https://github.com/Bhavya031/Glance) | Counts the exact starting context bytes that Prism assembles; it does not pick files or estimate the whole worker run. |
| [Neatlogs](https://neatlogs.com/) | Optional tracing for Prism's model workflow when `NEATLOGS_API_KEY` is set. It does not determine scope, usage, cost, or allocation. |
| [CFO.ai](https://cfo.ai/) | Supplies the configured budget and request tables through model tools, and stores Prism's reservation row. Prism code applies the budget policy. |

## Install

Follow [the setup guide](docs/SETUP.md) or run [`bash scripts/setup-github.sh /path/to/your/repo`](scripts/setup-github.sh) from a Prisme checkout. The guide covers credentials, the ready Entire mirror, CFO.ai table shape, and the rotating CFO.ai refresh token. Commit and push the generated config and workflows to the target repository's default branch.

## Configuration

The workflows read `.github/prism.json` from the target repository's default branch. The [`examples/prism.json`](examples/prism.json) shape has two groups:

| Group | Keys |
|---|---|
| `estimate` | `cooldown_minutes`, `provider`, `worker_model`, `max_cost_usd`, `max_output_tokens`, `max_extra_rounds`, `timeout_seconds` control the estimate and its file-discovery limits. |
| `cfo` | `org_id`, `scenario`, `page`, `budget_table`, `request_table`, `column_prefix` name the CFO.ai tables and columns to read and write. |

See [SETUP.md](docs/SETUP.md) for each field, secret, and required column. The optional Actions variable `PRISM_REPLAY_REF` pins a historical branch, tag, or commit; without it, the workflow uses the repository default branch.

## What appears on the issue

- **Estimate comment:** one marked comment is first reserved as “Estimating,” then updated with predicted files (**scope**), measured starting context and low/central/high projected tokens (**usage**), solve and end-to-end USD estimates (**cost**), discovery spend, worker model, and pinned commit. A failure updates it with the stopping reason when possible.
- **Allocation comment:** on a successful estimate, a separate comment mentions the requester and shows the repository budget, spent and reserved amounts, this request's cap, remaining balance, and maximum per request. It says allocated or blocked and includes the request ID. A denied policy check is reported as blocked; no reservation is written.

## CFO.ai data boundary

Prism sends CFO.ai the repository name, issue URL, pinned commit, worker model, projected central token counts, and finished central USD estimate, alongside the budget and policy fields already in the configured tables. It reads the matching budget row and existing request rows; Prism code applies the balance and per-request fraction rule and writes an allocation row when allowed. **Prism prices the tokens before this step.** CFO.ai receives no price list, repository code, or issue text from this allocation call.

## Limits and not built yet

- The low/central/high usage and cost projections use assumptions that have not been calibrated against a real coding-worker run. Model API charges for discovery can occur before a cost limit stops further calls.
- Scope predicts existing files only. A live issue needs a ready Entire mirror; a missing or failed index stops the run. Selected context excludes files that are missing or not text at the pinned commit.
- The example workflows estimate and reserve budget; they do not launch a solve worker. **Actual-spend report-back and reconciliation are not built into this workflow yet.**
- CFO.ai refresh tokens rotate. The workflows check secret-write access before refresh and serialize allocation runs; keep that concurrency group when copying them.

## Sponsors

Thanks to the projects whose tools make the scope, usage, and cost workflow possible:

| | |
|---|---|
| <img src="https://raw.githubusercontent.com/Bhavya031/Glance/main/assets/logo.png" alt="Glance logo" width="48"> | [Glance](https://github.com/Bhavya031/Glance) |
| <img src="https://avatars.githubusercontent.com/u/33188652?v=4" alt="Entire logo" width="48"> | [Entire](https://entire.io/) |
| <img src="https://neatlogs.com/apple-icon.png" alt="Neatlogs logo" width="48"> | [Neatlogs](https://neatlogs.com/) |
| <img src="https://cfo.ai/apple-icon.png" alt="CFO.ai logo" width="48"> | [CFO.ai](https://cfo.ai/) |
| <img src="https://avatars.githubusercontent.com/u/76263028?v=4" alt="Anthropic logo" width="48"> | [Anthropic](https://www.anthropic.com/) |
