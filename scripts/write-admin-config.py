#!/usr/bin/env python3
"""Replace an admin configuration after its generator succeeds."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


def main():
    destination = Path(sys.argv[1])
    command = sys.argv[2:]
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as output:
        temporary = Path(output.name)
        try:
            os.fchmod(output.fileno(), 0o600)
            if stat.S_IMODE(os.fstat(output.fileno()).st_mode) != 0o600:
                sys.exit(f"Cannot set mode 0600 on a temporary file for {destination}")
            result = subprocess.run(command, stdout=output)
            if result.returncode:
                if result.returncode < 0:
                    return 128 - result.returncode
                return result.returncode
            output.close()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
