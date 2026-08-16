class AppendOnlyViolation(RuntimeError):
    """Raised when code attempts to mutate or delete a LedgerEntry."""
