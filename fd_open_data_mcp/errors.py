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


class FinalizedObservationError(Exception):
    """Raised when a write would overwrite an observation past its revision window.

    落实 ``fact-ledger-kernel`` R1（事实只追加不得改写）在观测接入面的具体形态：
    定型后的值不可被普通写入改写。确需更正时走 correction 通道（显式声明 + 理由），
    该通道照常落 ``observation_revisions`` 留痕。

    拒绝必须**指名坐标**——只报"有若干条已定型"等于让人自己去猜。
    """

    def __init__(self, message: str, coordinates: list[tuple] = None,
                 window: dict = None):
        super().__init__(message)
        self.coordinates = coordinates or []
        self.window = window or {}
