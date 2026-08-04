"""Gate package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gate-review")
except PackageNotFoundError:  # source checkout before an editable install
    __version__ = "0+unknown"
