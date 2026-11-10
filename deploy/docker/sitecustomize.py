"""Load the mounted secrets file into EVERY Python process in the container.

The app loads $ECOSYSTEM_ENV_FILE itself at startup (ecosystem.runtime).
One-off `docker exec ventoryn python ...` maintenance scripts do not go
through that, so without this they would miss VENTORYN_TOKEN_KEY and
friends. Same rules as the app's loader: KEY=VALUE lines, surrounding
quotes stripped, comments ignored, and variables already set in the
real environment always win. Values are never printed.
"""
import os

_path = os.environ.get("ECOSYSTEM_ENV_FILE")
if _path:
    try:
        with open(_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _key, _, _value = _line.partition("=")
                _key = _key.strip()
                if _key.startswith("export "):
                    _key = _key[7:].strip()
                if _key and _key not in os.environ:
                    os.environ[_key] = _value.strip().strip("'\"")
    except OSError:
        pass
