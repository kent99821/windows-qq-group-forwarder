from __future__ import annotations

import os


def read_user_environment_variable(name: str) -> str | None:
    """Read a credential from the process environment, then the current user.

    PowerShell's ``$env:NAME`` is process-scoped and is intentionally checked
    first. If it is absent, read the persisted current-user value from HKCU.
    No separate machine-wide lookup is performed here.
    """
    process_value = os.environ.get(name)
    if isinstance(process_value, str) and process_value.strip():
        return process_value.strip()
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError):
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None
