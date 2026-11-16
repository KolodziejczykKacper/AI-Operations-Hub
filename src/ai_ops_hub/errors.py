"""Domain errors raised by input handling."""


class AIOpsError(Exception):
    """Base class for all application errors."""


class EmptyInputError(AIOpsError):
    """Input contained no usable content."""


class InputTooLargeError(AIOpsError):
    """Input exceeded a configured size limit."""


class MalformedMessageError(AIOpsError):
    """Input could not be interpreted as an email message."""
