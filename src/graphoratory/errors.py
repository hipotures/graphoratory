class GraphoratoryError(Exception):
    """Base class for errors that should be presented cleanly by the CLI."""


class ConfigurationError(GraphoratoryError):
    """Configuration is invalid."""


class IdentifierError(GraphoratoryError):
    """A typed identifier is invalid or cannot be resolved safely."""


class ArtifactError(GraphoratoryError):
    """An authoritative artifact is missing or invalid."""
