"""Custom exceptions for Artha Quant data and authentication pipelines."""

class ArthaQuantError(Exception):
    """Base exception for all domain errors."""
    pass


class KiteSessionExpiredError(ArthaQuantError):
    """Raised when Kite Connect access token is expired, missing, or invalid."""
    pass


class KiteRateLimitError(ArthaQuantError):
    """Raised when Kite historical API rate limits are violated."""
    pass


class DataIntegrityError(ArthaQuantError):
    """Raised when fetched market data fails sanity or OHLC integrity checks."""
    pass
