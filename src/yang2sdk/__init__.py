"""YANG2SDK tool suite."""

from importlib.metadata import PackageNotFoundError, version

# Single source of truth for the version is `pyproject.toml`
# ([project] version). This resolves it from installed metadata at runtime
# so the two can never drift. `hatch-vcs` was deliberately not adopted:
# tags in this repo are not semver and git-coupling every downstream build
# is not worth it pre-1.0.
try:
    __version__ = version("yang2sdk")
except PackageNotFoundError:  # source checkout without an install
    __version__ = "unknown"
