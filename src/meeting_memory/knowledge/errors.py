"""Domain-specific errors and CLI exit classifications."""


class KnowledgeError(Exception):
    """Base class for expected durable-knowledge failures."""


class ConfigurationError(KnowledgeError):
    """Configuration is missing or invalid."""


class ExtractionError(KnowledgeError):
    """Semantic extraction failed deterministically."""


class TransientExtractionError(ExtractionError):
    """The provider failure may succeed when retried."""


class AuthenticationError(ExtractionError):
    """Provider authentication or account state prevents extraction."""


class SchemaError(KnowledgeError):
    """Structured data did not match the Version 1 schema."""


class EvidenceError(KnowledgeError):
    """Evidence is missing, unsafe, or does not match a source."""


class StorageError(KnowledgeError):
    """A validated change could not be stored safely."""


class InvalidInputError(KnowledgeError):
    """A user-supplied value cannot produce a valid deterministic result."""
