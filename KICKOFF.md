# Prism kickoff prompts (paste as-is)

Copy this whole `prism-handoff` folder into the root of the new Prism repo first. Then paste one of the prompts below.

---

## Option 1: one coordinator agent (it spawns the others)

```
You are the lead engineer for Prism. Read HANDOFF.md in this repo completely before doing anything, then read reference/entire_compare.py from the line `MANIFEST = ...` to the end.

Your job:
1. Do step 0 from HANDOFF.md section 9 yourself: create the package skeleton, src/prism/types.py with the exact contracts, and stub functions that raise NotImplementedError. Copy data/ into the package's data/ folder. Make `prism --help` run.
2. Then start workstreams A, B, C, D, E and F from section 9 in parallel, one agent each. Give each agent: the path to HANDOFF.md, its workstream ID, the files it owns, its "Done when" line, and the rule that it edits only its own files.
3. When all six report done, do the integration step (I): wire everything together, run `prism dry-run` (free) and fix any leak or error.
4. Stop and ask me before any paid model call. Tell me the expected cost first. Expected costs: C's smoke test is under $0.01, `prism benchmark --one` is about $0.04, and the full benchmark is about $0.18.

Rules: follow HANDOFF.md section 2 exactly. If anything is unclear or contradicts the brief, ask me instead of assuming. When you finish, report what's done, what each check showed, and anything still open.
```

---

## Option 2: one agent per workstream (if your framework spawns them for you)

Run step 0 first and wait for it to finish:

```
Read HANDOFF.md completely. Do only step 0 of section 9: create the package skeleton, src/prism/types.py with the exact contracts from section 9, and stubs for every function in the contracts table that raise NotImplementedError. Copy data/ into the package. Make `prism --help` run. Don't implement anything else. Report the file tree when done. Ask me if anything is unclear.
```

Then paste this into each parallel agent, replacing `<ID>` with A, B, C, D, E or F:

```
Read HANDOFF.md completely, then reference/entire_compare.py from `MANIFEST = ...` to the end. You own workstream <ID> in HANDOFF.md section 9. Implement only the files listed for <ID>, porting the logic named in the contracts table. Don't edit any other file. If you need a contract change, stop and ask me. Follow section 2's rules. Don't make paid model calls unless your workstream's "Done when" needs one, and ask me first. Finish when your "Done when" line is true, and report what you ran to check it.
```

Then the integration agent:

```
Read HANDOFF.md completely. Workstreams A–F are done. You are workstream I: wire the modules together and fix integration bugs only. Run `prism dry-run` and confirm no target PR number appears in any context. Then ask me before running `prism benchmark --one` (about $0.04), and check it reproduces pytest recall 0.38 for both models. Report results with recall, precision, tokens, estimated cost and Neatlogs trace IDs.
```
