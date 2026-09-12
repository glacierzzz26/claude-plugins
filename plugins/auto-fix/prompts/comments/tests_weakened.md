**auto-fix aborted: the change removed test assertions.**

The diff deletes assertions or test definitions from test files:

{files}

No PR was opened. Making tests pass by removing them is not a fix. If the test
was genuinely wrong, a human should change it deliberately and explain why.
