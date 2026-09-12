**auto-fix stopped after {rounds} rounds without converging.** A human needs to
take over.

The fixer and the reviewer went back and forth for the maximum of {rounds}
rounds without reaching agreement, so **no pull request was opened**. The work
so far is on the branch `{branch}`, which has been pushed.

### What the reviewer still objected to

{unresolved}

### What to do

The automated loop is not going to resolve this on its own — further rounds
would just repeat the same disagreement. Options:

1. Check out `{branch}` and finish the change by hand, then open a PR.
2. Edit the issue to clarify what "fixed" means, then remove and re-apply the
   `auto-fix` label to try again.
3. Close the issue if the disagreement reflects a real ambiguity in the
   requirements.
