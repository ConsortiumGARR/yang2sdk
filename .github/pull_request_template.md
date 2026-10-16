## What

<!-- One or two sentences. What changes, and why now? -->

## Why

<!-- The problem this solves. If it fixes a bug, link the issue. -->

## Checklist

- [ ] The five gate commands pass (ruff, ruff format, ty, pyrefly, offline tests)
- [ ] `AGENTS.md` still describes the behaviour accurately, if the change touches
      the generator contract, the parity table, or a safety default
- [ ] New IR fields have template rendering **and** a generated sample that imports
- [ ] Protocol changes cite an RFC section (`RFCs/*.md`)
- [ ] No credential, device IP, or `temp/` output committed

## Evidence

Per `AGENTS.md`'s evidence rule — this is the part reviewers rely on most.

**Files changed:**

**Commands run:**

<!-- e.g. the five gate commands; `uv run yang2sdk --protocol netconf --device srl --list-roots` -->

**Verified:**

**Not verified / WIP:**

<!-- Be explicit. If you did not run the simulator matrix or a lab job, say so.
     A "skip" is not a pass, and unverified work presented as verified is the
     one failure mode this repo cannot recover from. -->

## Safety

- [ ] No secure default weakened (`verify=True`, `auto_commit=False`,
      `replace()` completeness guard, no embedded credentials)
- [ ] Any device quirk is documented and isolated to a downstream adapter, not
      baked into the generic path