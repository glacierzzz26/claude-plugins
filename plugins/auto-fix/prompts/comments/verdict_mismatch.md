**auto-fix could not complete: the review did not match the artifact.**

The reviewer returned a verdict for a different commit or diff than the one it
was given, twice in a row. No PR was opened, because approving an artifact that
was never actually reviewed would be worse than stopping.

This indicates a bug or a misconfiguration (for example, the working tree
changed between the diff and the review). Please report it with the run log.
