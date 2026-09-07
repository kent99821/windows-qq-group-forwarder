from __future__ import annotations

import os


def read_user_environment_variable(name: str) -> str | None:
    """Read a persisted user-scoped environment variable.

    ``os.environ`` is the merged process environment on Windows, so it cannot
    distinguish HKCU values from HKLM values. NapCat credentials must not
    silently come from the machine-wide environment; query HKCU directly.
    """
    if os.name != "nt":
        return os.environ.get(name)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError):
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None
