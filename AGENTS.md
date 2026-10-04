# Development rules

- Use uv for Python environments and dependencies. Run `uv run --locked python -m unittest discover -s tests -q` and the relevant checks before each functional commit.
- Preserve native Windows and POSIX support. Platform skips are not passes; test changed operating-system behavior on the relevant CI runners.
- Never commit credentials or print credential-helper output. Live model tests are optional and must use disposable fixtures and credentials from process environment variables.
- Keep writes and command execution subject to fresh explicit approval. Read-only mode, cancellation, path restrictions, output limits, and session privacy must be enforced by code, independent of model instructions.
- Commit and push in coherent functional batches. Small updates must pass local checks and use `[skip ci]` in the commit message to conserve Actions minutes. Accumulate changes into substantial updates before running CI; changes to native OS behavior need the relevant CI runners before claiming cross-platform validation. For each CI-enabled push, locate the workflow run for that exact commit SHA and wait until all required jobs finish successfully. Fix failed jobs before the next CI-enabled feature batch. Clearly label commits without CI; never report a green result from another commit or from local tests.
