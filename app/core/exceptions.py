"""
app/core/exceptions.py

Custom exception hierarchy for the RC SMS Webhook integration.
All exceptions carry enough context for structured logging.
"""


class RCSMSWebhookBaseError(Exception):
    """Base class for all application-specific errors."""

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.message = message
        self.context = context


class RCValidationError(RCSMSWebhookBaseError):
    """
    Raised when an incoming request fails RingCentral authentication.
    Triggers a 401 response so RC knows the request was rejected.
    """


class DuplicateMessageError(RCSMSWebhookBaseError):
    """
    Raised when an inbound message ID has already been processed
    within the idempotency TTL window.
    """


class ZapierForwardError(RCSMSWebhookBaseError):
    """
    Raised when all retry attempts to the Zapier webhook have been
    exhausted. Carries the last HTTP status code and attempt count.
    """

    def __init__(
        self,
        message: str,
        attempts: int,
        last_status_code: int | None = None,
        **context,
    ):
        super().__init__(message, **context)
        self.attempts = attempts
        self.last_status_code = last_status_code


class PayloadParseError(RCSMSWebhookBaseError):
    """Raised when the RC event payload cannot be parsed into our schema."""
