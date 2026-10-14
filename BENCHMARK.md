<h1 align="center">Superintelligence needs a budget.</h1>
<p align="center"><strong>On 30 real GitHub issues, Prism found 82% of the files the real fix changed, using Entire search, at about 2 cents per estimate.</strong><br><sub>Claude Sonnet; mean across two repeats per issue.</sub></p>
<p align="center">Powered by</p>
<p align="center">
  <a href="https://cfo.ai/"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/logos/cfo-dark.svg"><img src="docs/assets/logos/cfo-light.svg" alt="CFO.ai" height="30"></picture></a>&nbsp;&nbsp;&nbsp;
  <a href="https://entire.io/"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/logos/entire-dark.svg"><img src="docs/assets/logos/entire-light.svg" alt="Entire" height="30"></picture></a>&nbsp;&nbsp;&nbsp;
  <a href="https://neatlogs.com/"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/logos/neatlogs-dark.svg"><img src="docs/assets/logos/neatlogs-light.svg" alt="neatlogs" height="30"></picture></a>
</p>

![Prism: automated estimation and allocation](docs/assets/prism-poster.webp)

## File prediction, measured

One issue goes in. Prism predicts the existing files a fix will touch. The score compares those predictions with the files changed by the real merged fix.

| | Claude Sonnet | OpenAI GPT-6 Sol |
|---|---:|---:|
| **Recall** | **81.8%** | **76.5%** |
| **Precision** | **66.7%** | **64.8%** |
| **Exact file sets** | **10 / 60** | **9 / 60** |
| **Total discovery cost** | **$1.30** | **$0.88** |
| **Cost / estimate** | **2.16¢** | **1.47¢** |

Recall and precision are the means of per-issue scores across both repeats. Costs are token-based list-price estimates for file discovery.

## How it was run

- **5 frozen snapshot repos** in [prism-benchmarks](https://github.com/prism-benchmarks), each fixed at an upstream main-branch commit.
- **30 real closed issues** whose fixing PRs merged after their project snapshot; ground truth is the modified files already present in that snapshot.
- **Entire code search only** on the frozen mirror, with no search fallback; 120 / 120 saved predictions record `discovery.source=entire`.
- **2 repeats × 2 models** for each issue, scored against the same fix.
- **Every run traced in NeatLogs:** 120 unique trace IDs; the first trace for each provider was read back before the remaining runs.

![How Prism works](docs/assets/prism-flow.svg)

## By project

| Frozen snapshot | Issues | Claude recall / precision | OpenAI recall / precision |
|---|---:|---:|---:|
| [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 6 | 80.0% / 60.1% | 76.8% / 63.6% |
| [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 6 | 91.7% / 41.2% | 83.3% / 36.9% |
| [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 6 | 77.8% / 65.3% | 61.1% / 63.3% |
| [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 6 | 66.7% / 88.9% | 66.7% / 86.1% |
| [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 6 | 93.1% / 77.8% | 94.4% / 74.0% |

<details>
<summary><strong>All 30 issues and real fixes</strong> — recall by model, repeat 1 / repeat 2</summary>

| Issue | Fix PR | Frozen snapshot | Claude recall | OpenAI recall |
|---|---|---|---:|---:|
| [flask #3193](https://github.com/pallets/flask/issues/3193) | [#6133](https://github.com/pallets/flask/pull/6133) | [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 33% / 33% | 0% / 0% |
| [flask #6093](https://github.com/pallets/flask/issues/6093) | [#6096](https://github.com/pallets/flask/pull/6096) | [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 100% / 100% | 100% / 100% |
| [flask #5961](https://github.com/pallets/flask/issues/5961) | [#5962](https://github.com/pallets/flask/pull/5962) | [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 100% / 100% | 100% / 100% |
| [flask #5916](https://github.com/pallets/flask/issues/5916) | [#5917](https://github.com/pallets/flask/pull/5917) | [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 71% / 71% | 86% / 86% |
| [flask #5816](https://github.com/pallets/flask/issues/5816) | [#5899](https://github.com/pallets/flask/pull/5899) | [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 100% / 100% | 100% / 100% |
| [flask #5895](https://github.com/pallets/flask/issues/5895) | [#5898](https://github.com/pallets/flask/pull/5898) | [flask@ad68a12645d9](https://github.com/prism-benchmarks/flask/tree/ad68a12645d96fe51558da13e18544ba458d7af0) | 75% / 75% | 75% / 75% |
| [requests #7432](https://github.com/psf/requests/issues/7432) | [#7433](https://github.com/psf/requests/pull/7433) | [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 100% / 100% | 100% / 100% |
| [requests #4795](https://github.com/psf/requests/issues/4795) | [#7427](https://github.com/psf/requests/pull/7427) | [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 100% / 100% | 100% / 100% |
| [requests #7375](https://github.com/psf/requests/issues/7375) | [#7376](https://github.com/psf/requests/pull/7376) | [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 100% / 100% | 100% / 100% |
| [requests #6810](https://github.com/psf/requests/issues/6810) | [#7310](https://github.com/psf/requests/pull/7310) | [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 100% / 100% | 100% / 100% |
| [requests #5436](https://github.com/psf/requests/issues/5436) | [#7244](https://github.com/psf/requests/pull/7244) | [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 100% / 100% | 0% / 100% |
| [requests #7219](https://github.com/psf/requests/issues/7219) | [#7220](https://github.com/psf/requests/pull/7220) | [requests@70298332899f](https://github.com/prism-benchmarks/requests/tree/70298332899f25826e35e42f8d83425124f755a5) | 50% / 50% | 50% / 50% |
| [click #3802](https://github.com/pallets/click/issues/3802) | [#3818](https://github.com/pallets/click/pull/3818) | [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 67% / 67% | 67% / 67% |
| [click #2869](https://github.com/pallets/click/issues/2869) | [#3781](https://github.com/pallets/click/pull/3781) | [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 100% / 100% | 100% / 100% |
| [click #2582](https://github.com/pallets/click/issues/2582) | [#3780](https://github.com/pallets/click/pull/3780) | [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 0% / 0% | 0% / 0% |
| [click #3571](https://github.com/pallets/click/issues/3571) | [#3769](https://github.com/pallets/click/pull/3769) | [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 100% / 100% | 100% / 100% |
| [click #3135](https://github.com/pallets/click/issues/3135) | [#3819](https://github.com/pallets/click/pull/3819) | [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 100% / 100% | 50% / 50% |
| [click #1054](https://github.com/pallets/click/issues/1054) | [#3820](https://github.com/pallets/click/pull/3820) | [click@047adef258fc](https://github.com/prism-benchmarks/click/tree/047adef258fc25566163ffc3efd14effc0ef7352) | 100% / 100% | 0% / 100% |
| [pytest #12223](https://github.com/pytest-dev/pytest/issues/12223) | [#15150](https://github.com/pytest-dev/pytest/pull/15150) | [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 100% / 100% | 100% / 100% |
| [pytest #12175](https://github.com/pytest-dev/pytest/issues/12175) | [#15149](https://github.com/pytest-dev/pytest/pull/15149) | [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 67% / 67% | 67% / 67% |
| [pytest #6750](https://github.com/pytest-dev/pytest/issues/6750) | [#15058](https://github.com/pytest-dev/pytest/pull/15058) | [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 67% / 67% | 67% / 67% |
| [pytest #15052](https://github.com/pytest-dev/pytest/issues/15052) | [#15053](https://github.com/pytest-dev/pytest/pull/15053) | [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 33% / 33% | 33% / 33% |
| [pytest #15043](https://github.com/pytest-dev/pytest/issues/15043) | [#15044](https://github.com/pytest-dev/pytest/pull/15044) | [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 67% / 67% | 67% / 67% |
| [pytest #11783](https://github.com/pytest-dev/pytest/issues/11783) | [#15040](https://github.com/pytest-dev/pytest/pull/15040) | [pytest@3b17fa79f8ad](https://github.com/prism-benchmarks/pytest/tree/3b17fa79f8ad28bbda7aa468a750605be9c24553) | 67% / 67% | 67% / 67% |
| [pydantic #13971](https://github.com/pydantic/pydantic/issues/13971) | [#13980](https://github.com/pydantic/pydantic/pull/13980) | [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 100% / 100% | 100% / 100% |
| [pydantic #13976](https://github.com/pydantic/pydantic/issues/13976) | [#13978](https://github.com/pydantic/pydantic/pull/13978) | [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 100% / 50% | 100% / 100% |
| [pydantic #13965](https://github.com/pydantic/pydantic/issues/13965) | [#13972](https://github.com/pydantic/pydantic/pull/13972) | [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 100% / 100% | 100% / 100% |
| [pydantic #13919](https://github.com/pydantic/pydantic/issues/13919) | [#13967](https://github.com/pydantic/pydantic/pull/13967) | [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 100% / 100% | 100% / 100% |
| [pydantic #13957](https://github.com/pydantic/pydantic/issues/13957) | [#13964](https://github.com/pydantic/pydantic/pull/13964) | [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 100% / 100% | 100% / 100% |
| [pydantic #13950](https://github.com/pydantic/pydantic/issues/13950) | [#13951](https://github.com/pydantic/pydantic/pull/13951) | [pydantic@f512b087202f](https://github.com/prism-benchmarks/pydantic/tree/f512b087202f58ec90a9d38c9102858567d72440) | 67% / 100% | 67% / 67% |

</details>

## What this does not establish

- The sample covers five Python projects. It does not establish accuracy across other languages, project sizes, or issue types.
- An agent chose the issues using the stated closed-issue, merged-fix, and existing-file rules. This is a selected sample, not a random draw.
- This run has no without-Entire baseline, so it does not measure the effect of switching the search source.
- It measures file prediction, not whether a coding agent would complete the fix. Discovery costs are estimates, not provider invoice totals.

**[Explore the complete results and trace IDs →](data/results/entire-benchmark.json)**
