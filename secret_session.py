"""Process-local fallback state shared by the desktop and chat settings modules."""

SESSION_SECRETS: dict[tuple[str, str], str] = {}
STORAGE_ERRORS: dict[tuple[str, str], str] = {}

