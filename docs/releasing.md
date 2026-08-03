# Release process

Releases use isolated build and publish jobs with PyPI Trusted Publishing.
Long-lived PyPI API tokens are not required.

See the official
[PyPI Trusted Publishing guide](https://docs.pypi.org/trusted-publishers/)
for index-side setup and security considerations.

## One-time repository setup

Create protected GitHub environments named `testpypi` and `pypi`. Require
reviewers for the production environment.

Configure a Trusted Publisher on each index with:

- Owner: `ogunkirikci`
- Repository: `fastapi-lens`
- TestPyPI workflow: `publish-testpypi.yml`
- PyPI workflow: `publish-pypi.yml`
- Environment: `testpypi` or `pypi`

The workflow filename and environment must exactly match the publisher
configuration.

## Prepare a release

1. Update the version in `pyproject.toml` and `src/fastapi_lens/__init__.py`.
2. Move relevant changelog entries from `Unreleased` into a dated version.
3. Run the full test, lint, format, type, build, and metadata checks.
4. Build from a clean worktree and inspect both the wheel and source archive.
5. Commit the release preparation.

Local verification:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build
uv run twine check dist/*
```

## TestPyPI

Run the `Publish to TestPyPI` workflow manually from the intended commit on
`main`. The workflow builds in a job without OIDC permission, transfers the
immutable distributions as an artifact, and grants `id-token: write` only to
the protected publish job.

Install from TestPyPI in an isolated environment and run the quick-start smoke
application before continuing.

## PyPI

1. Create and push a signed tag matching the package version, such as
   `v0.1.0a1`.
2. Create a GitHub release for that tag.
3. The `Publish to PyPI` workflow verifies that the tag and package version
   match, builds and checks the distributions, then pauses at the protected
   `pypi` environment.
4. Approve the environment deployment after verifying the artifact and
   TestPyPI smoke result.

Published filenames are immutable. Do not reuse a version. If a release is
incorrect, increment the version and publish a new release.
