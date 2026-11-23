---
title: Plan and spec
---

# Plan and spec

Two documents in the target repository drive every run. The **spec** says what the system must be
and stays fixed; the **plan** is the work queue and changes after every pass. This page describes
where they live, the status block the engine parses, and how to add or reopen work.

## Where the files live

Everything project-specific is under `.mae/` in the repository being built:

```text
<repo>/.mae/
├── spec.md        architecture, the default for --spec
├── plan.md        work queue with the ENGINE-STATUS block, the default for --plan
├── config.json    per-repo settings (optional)
├── prompts/       per-repo prompt overrides (optional)
└── runs/          one JSONL log per run (add it to .gitignore)
```

The engine ships no documents of its own, so one installation serves any number of projects. The
defaults are `<repo>/.mae/spec.md` and `<repo>/.mae/plan.md`. Move them with `--spec` and `--plan`,
or with the `spec` and `plan` keys in `.mae/config.json`. See [Configuration](../reference/config.md)
and [Command line](../reference/cli.md).

The directories containing the spec and the plan are automatically readable by the coder. Commit
`.mae/` with the project, but keep `.mae/runs/` out of git.

## The spec is frozen

The spec holds the requirements, interfaces, data models and invariants. It is the authority: the
coder is told that when the code and the spec disagree, the spec wins unless the work queue says
otherwise. Only you edit it. The engine never writes to it.

Because the spec is the stable part of every prompt, put durable facts there and put changing work
in the plan. When the architecture changes, edit the spec between runs, then adjust the plan.

## The plan holds the status block

The engine reads a single HTML comment in the plan, called the status block:

```markdown
<!-- ENGINE-STATUS
Login flow: DONE
Password reset: IN_PROGRESS
Rate limiting: TODO — changed the window to 60s
Payment webhook: BLOCKED missing the provider's sandbox keys
-->

# Notes
Everything outside the block is yours. The coder is told not to rewrite or reorder it.
```

Inside the block, one line per unit of work:

```text
<name>: <STATE> <optional free-form note>
```

The state must be one of the four words below, in English and upper case. The name can be in any
language and may itself contain colons and dashes. The note after the state is free-form, is kept,
and is read by the coder.

## States

| State | Meaning | Engine behaviour |
|---|---|---|
| `TODO` | Not started, or reopened by you | Works on it |
| `IN_PROGRESS` | Started but not finished | Works on it |
| `DONE` | The code is written and verified | Leaves it alone |
| `BLOCKED` | Needs a human: credentials, data, a product decision | Stops retrying; write the reason on the line |

`TODO` and `IN_PROGRESS` are the open states. A plan is finished when it has at least one line and
none of them is open. `BLOCKED` counts as finished for that purpose — the loop cannot unblock it —
but a clean run that still has blocked units exits `2`, so a human has to look.

`DONE` means verified, not merely written: the coder's instructions require the unit's tests to
exist and to have passed before it flips the line. A `DONE` unit without a test is treated as not
done and comes back.

## Parsing rules

The engine reads the block with two regular expressions. These are the rules that follow from them.

- The block starts with `<!-- ENGINE-STATUS` and ends at the **first** `-->`. Whitespace after
  `<!--` and after `ENGINE-STATUS` is allowed.
- Each non-empty line inside the block must match `<name>: <STATE>`, with optional leading
  whitespace. The name is everything before the colon that introduces a valid state, so
  `Step 6 — Orders: state machine: DONE` parses with the name `Step 6 — Orders: state machine`.
- A line whose state is misspelt or missing is ignored, not guessed. At startup you get
  `WARNING: unreadable status line, the engine ignores it: <line>`. After a coder pass the same
  problem makes the pass red, so the coder has to fix it.
- Only the first block is read. A second one produces
  `WARNING: <n> ENGINE-STATUS blocks — the engine reads only the first; merge them into one`.
- A `-->` inside a note closes the block early. Every unit after it becomes invisible, so write
  `→` instead. At startup the engine warns that a `-->` closed the block early.
- Order does not matter. The block is an index, not a sequence.
- Deleting the `<!-- ENGINE-STATUS` or `-->` markers erases the engine's memory of what is done.
  If the block disappears during a run that started with open units, the pass is red with
  `the ENGINE-STATUS block is gone`.
- A unit that was open when the run started must still be in the block after a pass. If it
  disappears, the pass is red: `unit '<name>' was open at run start and is no longer in the status
  block — restore it, or mark it BLOCKED with a reason`. This is why you should rename or delete an
  open unit between runs, not during one.
- An empty block is not a finished plan. The engine sees no states, treats the queue as not started,
  and lets the coder write the block from the spec and plan.
- One run per plan file. A second run on the same plan exits immediately with code `3` and
  `another run already holds <plan> — wait for it or kill it`.

The startup warnings are printed before the first agent starts. During a run, the same problems are
reported by the tester as `plan: ...` errors and turn the pass red.

## The audit reopens units

When the queue is closed and the gates are green, the auditor checks the code against the spec. It
reports a problem per unit. For each `DONE` unit with a finding, the engine rewrites that line once,
using the first finding as the note:

```text
Rate limiting: TODO — audit: the limit is per IP, not per user
```

It prints `[auditor] reopened: <names>` and the loop sends those units back to the coder. The
finding in the note is shortened to 160 characters, and any `-->` in it becomes `→` so the block
cannot be closed by accident. Only `DONE` units are reopened; `BLOCKED` units stay blocked, and a
finding that names no line is ignored for reopening but still appears in the audit result.

A `TODO — audit:` line is the auditor's finding. Do not mark it `DONE` by hand without fixing the
problem and adding a test that would have caught it: while the unit is in the run's audit scope, the
next audit reopens it. The coder is told to fix that exact finding.

The auditor's scope is the units that were open when the run started. If the run started without a
status block, its scope is every unit in the block the coder wrote. Earlier work is not re-reviewed
on every run.

## Adding and changing work

The spec is the content; the status block is the routing. The coder reads the whole spec, but only
runs when the block has something open. Editing only the prose, with every line `DONE`, changes
nothing.

| You want | Do this |
|---|---|
| A small change | Add one line: `Idempotent retries in the sync job: TODO` |
| To correct finished work | Reopen it with a note: `Rate limiting: TODO — per user, not per IP`. The coder reads a `TODO` over existing code as “reconcile with the new note”. |
| A new subsystem | Describe it in the spec (or below the block) and add its line. Use the same name in both places so the agent finds the detail. |
| To unblock a unit | Change `BLOCKED` to `TODO` once the credentials, data or decision exist. |
| A shorter block | Delete old `DONE` lines, but keep at least one line. An empty block reads as “never started” and the coder rewrites the plan from scratch. |

A plan with many units needs a higher pass ceiling than the default of 15. Set `--max-iterations`
above the number of open units; the engine stops at the ceiling and the next run continues from the
block. See [Command line](../reference/cli.md).

## Keeping the plan honest

The status block is the only state that survives between runs. Commit it with the project. If an
agent damages it, `git diff` shows you what changed. The coder is instructed to update the block
incrementally — finish one unit, flip it, then start the next — because a session can be killed at
any moment and the block on disk is what the next run inherits.

To see how the block routes the loop, read [The loop](../concepts/loop.md). To change the plan from a
browser instead of an editor, use the [Dashboard](dashboard.md).
