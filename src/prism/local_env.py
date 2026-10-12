"""Keep local credentials in the working directory's private .env file."""

import os
import re
import tempfile
from pathlib import Path


def present(name: str) -> bool:
    path = Path.cwd() / ".env"
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        key, separator, value = line.lstrip().removeprefix("export ").partition("=")
        if separator and key.strip() == name:
            return bool(value.strip())
    return False


def save(name: str, value: str) -> None:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or "\n" in value or "\r" in value:
        raise ValueError("invalid .env credential")
    path = Path.cwd() / ".env"
    if path.is_symlink():
        raise RuntimeError(".env is a symbolic link; choose a regular file before saving credentials")
    original = path.read_text() if path.exists() else ""
    lines = original.splitlines(keepends=True)
    replacement = f"{name}={value}\n"
    for index, line in enumerate(lines):
        if line.lstrip().removeprefix("export ").split("=", 1)[0].strip() == name:
            lines[index] = replacement
            break
    else:
        if original and not original.endswith("\n"):
            lines.append("\n")
        lines.append(replacement)
    fd, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("".join(lines))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    os.environ[name] = value
