"""Single source of truth for the package version.

Read this constant directly rather than ``importlib.metadata.version(...)``:
a frozen PyInstaller onefile binary carries no installed distribution metadata,
so a metadata lookup would raise ``PackageNotFoundError``. CI overwrites this
file from the git tag at build time (see ``.github/workflows/release.yml``).
"""

__version__ = "0.1.0"
