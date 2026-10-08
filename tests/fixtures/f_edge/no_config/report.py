"""Print a one-line disk usage report."""

import shutil

from cleanup import old_files


def report(directory: str) -> str:
    usage = shutil.disk_usage(directory)
    stale = len(list(old_files(directory)))
    return f"{usage.used}/{usage.total} bytes used, {stale} stale files"
