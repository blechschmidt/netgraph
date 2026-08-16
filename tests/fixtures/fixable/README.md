An inventory whose only problems are ones `netviz validate --fix` can repair.

Every other fixture in `tests/fixtures/` is a single rule in isolation, which is
what a rule's unit test wants. This one is a small *tree* that is wrong in four
different ways at once, because that is what the fix loop has to cope with: each
repair is computed against the tree the previous one left behind, and two of
these findings live in the same document.

`tests/test_fixes.py` repairs it and re-validates; `docs/commands/validate.md`
shows the transcript.
