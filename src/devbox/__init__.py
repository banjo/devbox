class DevboxError(Exception):
    """An error that is shown to the user without a traceback.

    `details` are added to the JSON error object; `exit_code` is the process exit code.
    """

    def __init__(self, message: str, *, code: str | None = None, exit_code: int = 1, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}
