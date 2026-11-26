#!/usr/bin/env python3
"""Replace an admin configuration after its generator succeeds."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


@contextmanager
def private_temporary_file(destination):
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            os.fchmod(output.fileno(), 0o600)
            if stat.S_IMODE(os.fstat(output.fileno()).st_mode) != 0o600:
                sys.exit(
                    f"Cannot set mode 0600 on a temporary file for {destination}. "
                    "Use a filesystem that supports Linux file permissions."
                )
            yield output, temporary
        finally:
            temporary.unlink(missing_ok=True)


def main():
    if sys.argv[1] == "--check":
        for destination in map(Path, sys.argv[2:]):
            with private_temporary_file(destination):
                pass
        return 0

    destination = Path(sys.argv[1])
    with private_temporary_file(destination) as (output, temporary):
        result = subprocess.run(sys.argv[2:], stdout=output)
        if result.returncode:
            if result.returncode < 0:
                return 128 - result.returncode
            return result.returncode
        output.close()
        temporary.replace(destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
