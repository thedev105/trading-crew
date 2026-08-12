from datetime import datetime


class MissingPointInTimeRecordError(LookupError):
    """Raised when an explicit point-in-time lookup has no eligible evidence."""

    def __init__(self, lookup_key: tuple[str, ...], as_of: datetime) -> None:
        self.lookup_key = lookup_key
        self.as_of = as_of
        super().__init__(lookup_key, as_of)


__all__ = ["MissingPointInTimeRecordError"]
