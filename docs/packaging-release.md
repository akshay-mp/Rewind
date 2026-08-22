# Packaging and Release

TimeTravel publishes a Python wheel and source distribution. The wheel contains
the production Vite build under `timetravel/_ui`, so `pip install agent-timetravel` is
enough to run `timetravel ui`. A checkout still falls back to `web/dist` for local
development.

## Build locally

```bash
cd web && pnpm install --frozen-lockfile && pnpm build
cd ..
python -m pip install -e '.[dev]' build twine
python scripts/packaging_smoke.py
```

For an application-managed install, pin the published distribution in its
`requirements.txt`:

```text
agent-timetravel==0.1.2
```

Then install the application requirements with:

```bash
python -m pip install -r requirements.txt
```

The distribution name is `agent-timetravel`, but the Python import package and
public API remain `timetravel`:

```python
from agent_timetravel import TimeTravel, TimeTravelContext, timetravel
```

The `timetravel` CLI and the `timetravel` decorator object are unchanged. Optional
framework support is installed with an extra, for example
`agent-timetravel[adk]`.

The smoke test builds both artifacts, checks that the wheel contains
`timetravel/_ui` and the sdist contains `web/dist`, rejects environment files,
builds a wheel from the sdist, and runs the installed wheel from an isolated
temporary directory. It verifies package import, CLI help, and `/ui/` without
using the checkout's `web/dist`.

For a lighter archive check after a build:

```bash
python -m twine check dist/*
```

## Trusted Publishing setup

Complete this one-time setup before the first publish. Configure the project
name immediately because PyPI and TestPyPI availability can change.

1. In the GitHub repository, open **Settings → Environments** and create these
   environments:

   | Environment | URL | Use |
   |---|---|---|
   | `pypi` | `https://pypi.org/p/agent-timetravel` | Production release; add required reviewers if desired. |
   | `testpypi` | `https://test.pypi.org/p/agent-timetravel` | Manual pre-release verification. |

2. On PyPI, open <https://pypi.org/manage/account/publishing/> and add a
   **pending publisher** with exactly:

   - PyPI project name: `agent-timetravel`
   - GitHub owner: `akshay-mp`
   - GitHub repository: `TimeTravel`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

3. On TestPyPI, open
   <https://test.pypi.org/manage/account/publishing/> and add a pending
   publisher with the same owner and repository, workflow filename
   `testpypi.yml`, and environment name `testpypi`.

No PyPI token or GitHub secret is used. The publish jobs request the GitHub
OIDC identity token and the configured publisher binds it to the repository,
workflow, and environment above.

## Release

1. Build and verify the frontend and Python artifacts locally.
2. Update `project.version` in `pyproject.toml`.
3. Create and push the matching tag, for example `v0.1.2`.

The tag workflow fails before publishing when the tag version does not match
`project.version`. It publishes through PyPI Trusted Publishing with GitHub
Actions OIDC; no PyPI token or repository secret is required.

Production publication is available only from a pushed matching `vX.Y.Z` tag.
For a safe manual TestPyPI rehearsal, dispatch the dedicated workflow and type
the required confirmation:

```bash
gh workflow run testpypi.yml --ref main -f confirm=publish-to-testpypi
```

That workflow has only the `testpypi` repository URL and cannot publish to
production PyPI. Inspect the installed TestPyPI package before creating the
production tag.

After a manual TestPyPI publish completes, wait for the package to be visible
and verify it manually. TestPyPI does not necessarily contain all runtime
dependencies, so keep PyPI as the extra index:

```bash
RELEASE_VERSION=0.1.2  # Set this to project.version before each release.
python -m venv /tmp/timetravel-testpypi-verify
/tmp/timetravel-testpypi-verify/bin/python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple \
  "agent-timetravel==${RELEASE_VERSION}"
/tmp/timetravel-testpypi-verify/bin/python -c \
  "from importlib.metadata import version; from agent_timetravel import TimeTravel, TimeTravelContext, timetravel; assert version('agent-timetravel') == '${RELEASE_VERSION}'; print('TestPyPI import ok')"
/tmp/timetravel-testpypi-verify/bin/timetravel --version
```

This is a manual post-publish check only; the workflow does not automate a
propagation wait.
