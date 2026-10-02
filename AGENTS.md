# Development rules

- Use uv for Python environments and dependencies. Run `uv run --locked python -m unittest discover -s tests -q` and the relevant checks before each functional commit.
- Preserve native Windows and POSIX support. Platform skips are not passes; test changed operating-system behavior on the relevant CI runners.
- Never commit credentials or print credential-helper output. Live model tests are optional and must use disposable fixtures and credentials from process environment variables.
- Keep writes and command execution subject to fresh explicit approval. Read-only mode, cancellation, path restrictions, output limits, and session privacy must be enforced by code, independent of model instructions.
- Commit and push in coherent functional batches. After **every push**, locate the workflow run for that exact commit SHA and wait until all required jobs finish successfully. Fix failed jobs before pushing the next feature batch. Do not report a green CI result from another commit or from local tests.
