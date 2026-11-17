"""Domain errors raised by input handling."""


class AIOpsError(Exception):
    """Base class for all application errors."""


class EmptyInputError(AIOpsError):
    """Input contained no usable content."""


class InputTooLargeError(AIOpsError):
    """Input exceeded a configured size limit."""


class MalformedMessageError(AIOpsError):
    """Input could not be interpreted as an email message."""


class MessageNotFoundError(AIOpsError):
    """No stored message has the requested id."""


class ConflictNotAnalyzableError(AIOpsError):
    """Messages imported as conflicts are never analyzed automatically."""


class AnalyzerContractError(AIOpsError):
    """An analyzer returned a result that violates the analyzer contract."""
