"""Govnr ingestion and schema errors (used by parser, watcher, and UI)."""


class MissingFileError(FileNotFoundError):
    """A mandatory inbound CSV is missing."""

    pass


class SchemaDriftError(ValueError):
    """Required backend columns are absent after applying the client mapping."""

    pass
