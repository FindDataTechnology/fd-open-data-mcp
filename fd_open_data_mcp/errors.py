"""Custom exceptions for fd-open-data-mcp."""


class FetchError(Exception):
    """Raised when a data fetch operation fails."""
    
    def __init__(self, message: str, source: str = None, command: str = None):
        super().__init__(message)
        self.source = source
        self.command = command
    
    def __str__(self):
        if self.source and self.command:
            return f"{self.source}.{self.command}: {super().__str__()}"
        return super().__str__()
