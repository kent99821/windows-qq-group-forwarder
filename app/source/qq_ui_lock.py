from __future__ import annotations

import threading


# QQ history backfill and image copying both change the active conversation and
# foreground focus. Serializing them prevents one operation from reading or
# copying controls from the conversation opened by the other operation.
QQ_UI_LOCK = threading.RLock()
