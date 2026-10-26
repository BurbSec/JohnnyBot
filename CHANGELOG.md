# Changelog

Release notes for moderators are posted to the moderators channel from each
release's annotated git tag. This file is for whoever runs the bot.

## v1.2.1

### Security
- aiohttp 3.14.3 → 3.14.5, fixing eight advisories (three high):
  GHSA-2g87-pp6c-29x5, GHSA-5wr8-gcqf-gqf3, GHSA-9xw5-w5jj-pvcg,
  GHSA-494j-54xm-rwp3, GHSA-6cjc-53gc-5rqm, GHSA-pqxq-w44r-73xw,
  GHSA-f9xw-jh8m-3mwq and GHSA-gx76-c99x-2c96. `pyproject.toml` now
  requires `aiohttp>=3.14.4`, so the lock can never resolve back to a
  vulnerable version. Auto-update applies this on its own.

### Fixed
- The documented start command is now `uv run --frozen --no-dev bot.py`.
  v1.2.0 said `uv run bot.py`, which also installs the test dependencies
  on the host and, if that host's uv decides the lock is stale, rewrites
  `uv.lock` in the checkout — a local change that can make the next
  auto-update's `git pull --ff-only` fail. **If you followed the v1.2.0
  steps, update your service unit or start script.**

## v1.2.0

> [!CAUTION]
> ### ⚠️ BREAKING: dependencies are now managed with uv — manual upgrade required
>
> JohnnyBot no longer uses pip or `requirements.txt`. Dependencies are declared
> in `pyproject.toml` and pinned in `uv.lock`, and the bot is installed and run
> with [uv](https://docs.astral.sh/uv/).
>
> **Auto-update cannot perform this upgrade.** A bot running v1.1.x will pull
> this release, fail to reinstall from the now-deleted `requirements.txt`, post
> **"❌ Auto-update failed"** to the moderators channel, and keep running the
> old code until restarted by hand. That alert is expected — it is the old
> updater refusing to restart without its dependencies, not a crash.
>
> **To upgrade, on the host:**
>
> 1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`
>    (or `brew install uv`). Make sure it is on the `PATH`
>    of the account the bot runs as.
> 2. In the bot directory: `git pull --ff-only`
> 3. `uv sync --frozen --no-dev` — creates `.venv/` with the exact locked versions.
> 4. Start the bot with `uv run --frozen --no-dev bot.py` and
>    **update your systemd unit / start script to match**. Starting it with a
>    bare `python bot.py` uses whatever interpreter happens to be on the `PATH`,
>    not the synced venv.
>
> Once you are on v1.2.0, auto-update works again and re-syncs the venv itself
> on every update. If `uv` is missing, or the bot
> is not running from a virtual environment, auto-update stops and alerts
> instead of restarting into a broken environment.

### Why
The point of this release is a more dependable **auto-update**. Under pip it:
- reinstalled only the top-level pins, so transitive dependencies drifted
  between hosts and over time;
- never removed packages that had been dropped;
- installed into whatever interpreter the bot happened to run under — including
  a system Python, where pip is refused outright on distros that mark it
  externally managed (PEP 668);
- reinstalled only when GitHub's compare API listed `requirements.txt` among
  the changed files, so a failed or truncated compare restarted the bot
  without its new dependencies.

With uv, every update syncs the bot's own venv to `uv.lock` exactly — every
package, transitive ones included, added and removed — and refuses to restart
when it can't.

### Changed
- Installation and dependency management moved from pip (`requirements.txt`,
  `requirements-test.txt`) to uv (`pyproject.toml`, `uv.lock`). Test
  dependencies are now the `dev` dependency group: `uv sync && uv run pytest`.
- Auto-update runs `uv sync --frozen --no-dev` into the bot's own venv after
  every pull, in place of `pip install -r requirements.txt` when
  `requirements.txt` changed.
- CI installs with `uv sync --locked`, so a `pyproject.toml` change without a
  matching `uv.lock` update fails the build.

### Fixed
- Auto-update no longer deploys a release when GitHub's list of changed files
  can't be fetched or may be truncated (300+ files). That used to read as
  "`config_example.py` unchanged", so a release needing config changes could
  auto-deploy. It now posts a "review before updating" notice instead.

### Unchanged
- Every dependency stays at the same version as v1.1.1. This release changes
  how packages are installed, not which ones.

## Earlier releases

v1.1.1 and earlier predate this file; their notes are in the annotated tags
(`git tag -n99 v1.1.1`).
