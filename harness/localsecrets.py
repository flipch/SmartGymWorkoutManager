"""Local secret handling: mode-0600 env files and an atomically written token cache."""
import json
import os
import pathlib
import tempfile

CONFIG_DIR = pathlib.Path.home() / ".config" / "speediance-spike"


def load_env_file(path: pathlib.Path) -> dict:
    """Parse KEY=VALUE lines. Refuses files readable by group/other (they hold passwords)."""
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"missing {path}")
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must be chmod 600")
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = _unquote(value.strip())
    return env


def _unquote(value: str) -> str:
    """Remove ONE matching pair of surrounding quotes; a password may legitimately start or end with a quote."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_json_private(path: pathlib.Path) -> dict | None:
    """Read a JSON file we wrote ourselves; None if absent. Refuses over-permissive files."""
    path = pathlib.Path(path)
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must be chmod 600")
    return json.loads(path.read_text())


def write_json_private(path: pathlib.Path, data: dict) -> None:
    """Write JSON atomically with mode 0600 (tmp file in the same dir, then rename)."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())  # data on disk BEFORE the rename, or a power cut can leave an empty token file
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)  # make the rename itself durable
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
