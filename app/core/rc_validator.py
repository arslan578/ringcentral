"""
app/core/rc_validator.py

RingCentral webhook authentication utilities.

RC uses two mechanisms depending on how the subscription is created:
  1. Validation Challenge (GET):  RC sends ?validationToken=... → return it as plain text.
  2. Push Authentication (POST):  RC includes a "Verification-Token" header that must
                                   match the token you configured when creating the subscription.

Reference: https://developers.ringcentral.com/api-reference/Webhooks
"""
import hmac
import logging

from app.core.exceptions import RCValidationError

logger = logging.getLogger(__name__)


def validate_verification_token(
    received_token: str | None,
    expected_token: str,
) -> None:
    """
    Compare the token from the RC push header against our configured token.
    Uses `hmac.compare_digest` to prevent timing attacks.

    Raises:
        RCValidationError: if tokens don't match or no token present.
    """
    if not received_token:
        logger.warning(
            "RC webhook request missing Verification-Token header",
            extra={"event": "rc_auth_rejected", "reason": "missing_token"},
        )
        raise RCValidationError(
            "Missing Verification-Token header",
            reason="missing_token",
        )

    if not hmac.compare_digest(received_token.strip(), expected_token.strip()):
        logger.warning(
            "RC webhook Verification-Token mismatch",
            extra={"event": "rc_auth_rejected", "reason": "token_mismatch"},
        )
        raise RCValidationError(
            "Verification-Token header does not match expected value",
            reason="token_mismatch",
        )

    logger.debug("RC Verification-Token validated successfully")
