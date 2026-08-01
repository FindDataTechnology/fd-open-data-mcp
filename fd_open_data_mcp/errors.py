"""Custom exceptions for fd-open-data-mcp."""


class FetchError(Exception):
    """Raised when a data fetch operation fails."""
    
    def __init__(self, message: str, source: str = None, command: str = None):
        super().__init__(message)
        self.source = source
        self.command = command
    
    def __str__(self):
        if self.source and self.command:
            return f"{self.source}.{command}: {super().__str__()}"
        return super().__str__()


class SourceNotFoundError(FetchError):
    """Raised when the specified data source is not registered."""
    pass


class CommandNotFoundError(FetchError):
    """Raised when the specified command doesn't exist in the source."""
    pass


class RateLimitError(FetchError):
    """Raised when rate limiting is encountered."""
    pass
