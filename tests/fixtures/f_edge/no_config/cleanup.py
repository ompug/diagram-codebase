"""Delete temporary files older than a day."""

import os
import time


def old_files(directory: str, max_age: float = 86400.0):
    now = time.time()
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if now - os.path.getmtime(path) > max_age:
            yield path


def main() -> None:
    for path in old_files(os.environ.get("TMP_DIR", "/tmp")):
        os.remove(path)


if __name__ == "__main__":
    main()
