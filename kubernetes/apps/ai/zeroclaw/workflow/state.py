"""Durable automation state with atomic replacement and process-scoped locks."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def root():
    return Path(os.environ.get("AUTOMATION_STATE_DIR", "/data/state"))


def read(name):
    path = root() / (name + ".json")
    if not path.exists():
        raise ValueError("Automation state is missing; initialize it before running this workflow")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Unsupported automation state")
    return value


def write(name, value):
    directory = root()
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / (name + ".json"))
        directory_fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(name):
    root().mkdir(parents=True, exist_ok=True)
    with (root() / (name + ".lock")).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def initialize():
    for name in ("monitor", "github-hooks"):
        with lock(name):
            if (root() / (name + ".json")).exists():
                read(name)
            else:
                write(name, {"version": 1})
