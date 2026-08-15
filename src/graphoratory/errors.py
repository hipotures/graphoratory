class GraphoratoryError(Exception):
    """Base class for errors that should be presented cleanly by the CLI."""


class ConfigurationError(GraphoratoryError):
    """Configuration is invalid."""


class IdentifierError(GraphoratoryError):
    """A typed identifier is invalid or cannot be resolved safely."""


class ArtifactError(GraphoratoryError):
    """An authoritative artifact is missing or invalid."""


class InvalidGraphError(GraphoratoryError):
    """A graph violates the evaluator's scientific input contract."""


class BaselineFailure(GraphoratoryError):
    """The frozen baseline failed before producing a valid proposal."""


class EvaluationFailure(GraphoratoryError):
    """The independent evaluator could not produce sound score evidence."""
