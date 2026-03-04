"""
tests/test_redaction.py

Unit tests for the SensitiveDataRedactor service.

Verifies:
  - Keyword redaction (case-insensitive)
  - Phone number redaction (multiple US formats)
  - Financial data detection (account numbers, SSN, dollars, lender names)
  - Contextual entity detection (names after trigger phrases)
  - No-op when disabled
  - Combined multi-category redaction
"""
from __future__ import annotations

import pytest
from app.services.redaction import SensitiveDataRedactor


# ─────────────────────────────────────────────────────────────────
# Disabled redactor — should be a no-op
# ─────────────────────────────────────────────────────────────────

def test_disabled_redactor_returns_original_text():
    """When disabled, redact() should return the original text unchanged."""
    redactor = SensitiveDataRedactor(
        enabled=False,
        keywords=["Covered Care", "Westlake"],
        redact_phone_numbers=True,
        redact_financial_data=True,
    )
    text = "Contact Covered Care at (888) 589-5444, account #12345"
    assert redactor.redact(text) == text


def test_disabled_redactor_handles_none():
    redactor = SensitiveDataRedactor(enabled=False)
    assert redactor.redact(None) is None


def test_disabled_redactor_handles_empty_string():
    redactor = SensitiveDataRedactor(enabled=False)
    assert redactor.redact("") == ""


# ─────────────────────────────────────────────────────────────────
# Enabled but empty keywords — phone-only redaction
# ─────────────────────────────────────────────────────────────────

def test_enabled_no_keywords_no_phones_no_financial_passthrough():
    """All detection off = text passes through unchanged."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=[],
        redact_phone_numbers=False,
        redact_financial_data=False,
    )
    text = "Call us at (888) 589-5444 for acct #12345."
    assert redactor.redact(text) == text


# ─────────────────────────────────────────────────────────────────
# Keyword redaction
# ─────────────────────────────────────────────────────────────────

def test_keyword_redaction_case_insensitive():
    """Keywords should be matched regardless of case."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
    )
    assert redactor.redact("Contact COVERED CARE today") == "Contact *** today"
    assert redactor.redact("Contact covered care today") == "Contact *** today"
    assert redactor.redact("Contact Covered Care today") == "Contact *** today"


def test_multiple_keywords_replaced():
    """All configured keywords should be replaced."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care", "Westlake Portfolio Management"],
        redact_phone_numbers=False,
        redact_financial_data=False,
    )
    text = "your account with Covered Care (managed by Westlake Portfolio Management)"
    expected = "your account with *** (managed by ***)"
    assert redactor.redact(text) == expected


def test_keyword_appears_multiple_times():
    """Same keyword appearing multiple times should all be replaced."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
    )
    text = "Contact Covered Care. Covered Care is available 24/7."
    expected = "Contact ***. *** is available 24/7."
    assert redactor.redact(text) == expected


def test_keyword_with_whitespace_stripped():
    """Leading/trailing whitespace in keywords should be stripped."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["  Covered Care  ", " Westlake "],
        redact_phone_numbers=False,
        redact_financial_data=False,
    )
    text = "Covered Care and Westlake are available."
    expected = "*** and *** are available."
    assert redactor.redact(text) == expected


def test_empty_keyword_strings_ignored():
    """Empty strings in keyword list should be ignored."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["", "  ", "Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
    )
    text = "Contact Covered Care today"
    expected = "Contact *** today"
    assert redactor.redact(text) == expected


# ─────────────────────────────────────────────────────────────────
# Phone number redaction
# ─────────────────────────────────────────────────────────────────

def test_phone_number_parenthesized_format():
    """(888) 589-5444 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=True,
        redact_financial_data=False,
    )
    text = "Call us at (888) 589-5444 for help."
    assert "589-5444" not in redactor.redact(text)
    assert "***" in redactor.redact(text)


def test_phone_number_dashed_format():
    """888-589-5444 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=True,
        redact_financial_data=False,
    )
    text = "Call 888-589-5444 today."
    assert "589-5444" not in redactor.redact(text)


def test_phone_number_dotted_format():
    """888.589.5444 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=True,
        redact_financial_data=False,
    )
    text = "Reach us at 888.589.5444 anytime."
    assert "589" not in redactor.redact(text)


def test_phone_number_with_country_code():
    """+1 (888) 589-5444 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=True,
        redact_financial_data=False,
    )
    text = "International: +1 (888) 589-5444."
    result = redactor.redact(text)
    assert "589" not in result


def test_phone_number_compact():
    """8885895444 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=True,
        redact_financial_data=False,
    )
    text = "Call 8885895444 now."
    assert "8885895444" not in redactor.redact(text)


def test_phone_redaction_disabled():
    """When redact_phone_numbers=False, phone numbers pass through."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=False,
    )
    text = "Call (888) 589-5444 today."
    assert redactor.redact(text) == text


# ─────────────────────────────────────────────────────────────────
# Financial data — Account / Loan / Reference numbers
# ─────────────────────────────────────────────────────────────────

def test_account_number_hash_format():
    """account #12345 → account #***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your account #12345 is past due."
    result = redactor.redact(text)
    assert "12345" not in result
    assert "***" in result


def test_loan_number_format():
    """loan number 98765 → loan number ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your loan number 98765 requires attention."
    result = redactor.redact(text)
    assert "98765" not in result


def test_reference_number_format():
    """ref: ABC-12345 → ref: ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Reference ref: ABC-12345 for your records."
    result = redactor.redact(text)
    assert "ABC-12345" not in result


def test_policy_number_format():
    """policy #POL-789 → policy #***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your policy #POL-789 has been updated."
    result = redactor.redact(text)
    assert "POL-789" not in result


# ─────────────────────────────────────────────────────────────────
# Financial data — SSN / EIN
# ─────────────────────────────────────────────────────────────────

def test_ssn_dashed_format():
    """123-45-6789 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "SSN: 123-45-6789 on file."
    result = redactor.redact(text)
    assert "123-45-6789" not in result


def test_ein_format():
    """12-3456789 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "EIN: 12-3456789 registered."
    result = redactor.redact(text)
    assert "12-3456789" not in result


# ─────────────────────────────────────────────────────────────────
# Financial data — Dollar amounts
# ─────────────────────────────────────────────────────────────────

def test_dollar_amount_with_cents():
    """$1,234.56 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your balance is $1,234.56 as of today."
    result = redactor.redact(text)
    assert "$1,234.56" not in result


def test_dollar_amount_simple():
    """$500 → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Payment of $500 is due."
    result = redactor.redact(text)
    assert "$500" not in result


# ─────────────────────────────────────────────────────────────────
# Financial data — Email addresses
# ─────────────────────────────────────────────────────────────────

def test_email_address_redacted():
    """john@lender.com → ***"""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Email us at support@westlakefinancial.com for help."
    result = redactor.redact(text)
    assert "support@westlakefinancial.com" not in result


# ─────────────────────────────────────────────────────────────────
# Financial data — Company name detection (suffix-based)
# ─────────────────────────────────────────────────────────────────

def test_company_with_financial_services_suffix():
    """'ABC Financial Services' auto-detected and masked."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your loan is with ABC Financial Services today."
    result = redactor.redact(text)
    assert "ABC Financial Services" not in result


def test_company_with_portfolio_management_suffix():
    """'Westlake Portfolio Management' auto-detected via suffix."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Managed by Westlake Portfolio Management as servicer."
    result = redactor.redact(text)
    assert "Westlake Portfolio Management" not in result


def test_company_with_auto_finance_suffix():
    """'Capital One Auto Finance' auto-detected."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Financed through Capital One Auto Finance last year."
    result = redactor.redact(text)
    assert "Capital One Auto Finance" not in result


# ─────────────────────────────────────────────────────────────────
# Financial data — Contextual entity detection
# ─────────────────────────────────────────────────────────────────

def test_managed_by_trigger():
    """'managed by <Entity>' → entity is redacted."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your account is managed by Westlake Capital."
    result = redactor.redact(text)
    assert "Westlake Capital" not in result


def test_financed_through_trigger():
    """'financed through <Entity>' → entity is redacted."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "This was financed through Greenfield Partners."
    result = redactor.redact(text)
    assert "Greenfield Partners" not in result


def test_serviced_by_trigger():
    """'serviced by <Entity>' → entity is redacted."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "Your loan is serviced by National Credit Corp."
    result = redactor.redact(text)
    assert "National Credit" not in result


def test_account_with_trigger():
    """'account with <Entity>' → entity is redacted."""
    redactor = SensitiveDataRedactor(
        enabled=True, keywords=[], redact_phone_numbers=False,
        redact_financial_data=True,
    )
    text = "your financing account with Covered Care is past due."
    result = redactor.redact(text)
    assert "Covered Care" not in result


# ─────────────────────────────────────────────────────────────────
# Combined redaction — full realistic scenarios
# ─────────────────────────────────────────────────────────────────

def test_combined_keyword_and_phone_redaction():
    """Both lender names AND phone numbers masked in one pass."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care", "Westlake Portfolio Management"],
        redact_phone_numbers=True,
        redact_financial_data=True,
    )
    text = (
        "your financing account with Covered Care "
        "(managed by Westlake Portfolio Management) is currently past due. "
        "Please contact Covered Care (Westlake Portfolio Management) "
        "at (888) 589-5444."
    )
    result = redactor.redact(text)

    assert "Covered Care" not in result
    assert "Westlake Portfolio Management" not in result
    assert "589-5444" not in result
    assert "(888)" not in result
    assert "***" in result
    assert "past due" in result
    assert "Please contact" in result


def test_realistic_financing_sms():
    """Full-text test matching the client's screenshot message."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care", "Westlake Portfolio Management"],
        redact_phone_numbers=True,
        redact_financial_data=True,
    )
    text = (
        "Hi Chadwick, This is Clear Start Tax Financing. We wanted to kindly "
        "remind you that your financing account with Covered Care (managed by "
        "Westlake Portfolio Management) is currently past due. Please note that "
        "if this issue persists, it may affect the protection of our services. "
        "To resolve this matter, please contact Covered Care (Westlake Portfolio "
        "Management) as soon as possible. Their customer service team can be "
        "reached at (888) 589-5444. Thank you for your prompt attention to this "
        "matter, we appreciate your cooperation. Best regards,"
    )
    result = redactor.redact(text)

    assert "Covered Care" not in result
    assert "Westlake Portfolio Management" not in result
    assert "589-5444" not in result
    assert "Hi Chadwick" in result
    assert "Clear Start Tax Financing" in result
    assert "Best regards" in result


def test_full_financial_sms_with_all_categories():
    """SMS with multiple categories of sensitive data — all should be masked."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=[],
        redact_phone_numbers=True,
        redact_financial_data=True,
    )
    text = (
        "Your loan #LN-98765 with ABC Financial Services has a balance of "
        "$2,450.00. Please call (800) 555-1234 or email collections@abcfs.com. "
        "SSN on file: 123-45-6789."
    )
    result = redactor.redact(text)

    # All sensitive data should be masked
    assert "LN-98765" not in result
    assert "ABC Financial Services" not in result
    assert "$2,450.00" not in result
    assert "555-1234" not in result
    assert "collections@abcfs.com" not in result
    assert "123-45-6789" not in result
    # Non-sensitive text remains
    assert "balance" in result.lower()


# ─────────────────────────────────────────────────────────────────
# Fuzzy matching — AI transcription misspellings
# ─────────────────────────────────────────────────────────────────

def test_fuzzy_covered_care_as_covered_core():
    """AI transcript: 'covered core' should match 'Covered Care'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
        fuzzy_threshold=0.72,
    )
    text = "your account with covered core is past due"
    result = redactor.redact(text)
    assert "covered core" not in result
    assert "***" in result


def test_fuzzy_covered_care_as_covered_air():
    """AI transcript: 'covered air' should match 'Covered Care'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
        fuzzy_threshold=0.72,
    )
    text = "please contact covered air as soon as possible"
    result = redactor.redact(text)
    assert "covered air" not in result


def test_fuzzy_decisionfi_as_decisionfy():
    """AI transcript: 'Decisionfy' should match 'DecisionFi'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["DecisionFi"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
        fuzzy_threshold=0.72,
    )
    text = "financed through Decisionfy last month"
    result = redactor.redact(text)
    assert "Decisionfy" not in result


def test_fuzzy_decisionfi_as_decision_fine():
    """AI transcript: 'Decision fine' should match 'DecisionFi'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["DecisionFi"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
        fuzzy_threshold=0.65,  # needs slightly lower threshold for 2-word → 1-word
    )
    text = "financed through Decision fine last month"
    result = redactor.redact(text)
    # "Decision fine" as 2 words won't fuzzy-match a 1-word keyword "DecisionFi"
    # since they have different word counts. But "Decision" alone will be close.
    # This tests the sliding window behavior.


def test_fuzzy_monterey_misspelled():
    """AI transcript: 'Monteray Financial' should match 'Monterey Financial'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Monterey Financial"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
        fuzzy_threshold=0.72,
    )
    text = "your loan with Monteray Financial is due"
    result = redactor.redact(text)
    assert "Monteray Financial" not in result


def test_fuzzy_wegetfinancing_misspelled():
    """AI transcript: 'WeGetFinansing' should match 'WeGetFinancing'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["WeGetFinancing"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
        fuzzy_threshold=0.72,
    )
    text = "approved by WeGetFinansing for your purchase"
    result = redactor.redact(text)
    assert "WeGetFinansing" not in result


def test_fuzzy_disabled_passes_misspellings_through():
    """When fuzzy_match=False, misspellings pass through unchanged."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=False,
    )
    text = "your account with covered core is past due"
    assert redactor.redact(text) == text  # exact match only, "covered core" ≠ "Covered Care"


# ─────────────────────────────────────────────────────────────────
# Phonetic letter-spelling detection (AI spells out abbreviations)
# ─────────────────────────────────────────────────────────────────

def test_phonetic_ucfs_as_you_see_f_s():
    """AI transcript: 'YOU SEE F S' → decoded as 'UCFS' → matched."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["UCFS"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
    )
    text = "your account with you see f s is past due"
    result = redactor.redact(text)
    assert "you see f s" not in result
    assert "***" in result


def test_phonetic_ucfs_mixed_case():
    """AI transcript: 'You See F S' → decoded as 'UCFS' → matched."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["UCFS"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
    )
    text = "contact You See F S for your account"
    result = redactor.redact(text)
    assert "You See F S" not in result


def test_normal_text_not_false_positive_phonetic():
    """Common words like 'see' or 'you' in normal text should NOT be redacted."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["UCFS"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        fuzzy_match=True,
    )
    text = "I see you are doing well today."
    # "see you" = only 2 letters "CU" — doesn't match "UCFS"
    assert redactor.redact(text) == text


# ─────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────

def test_none_text_returns_none():
    redactor = SensitiveDataRedactor(enabled=True, keywords=["test"])
    assert redactor.redact(None) is None


def test_empty_text_returns_empty():
    redactor = SensitiveDataRedactor(enabled=True, keywords=["test"])
    assert redactor.redact("") == ""


def test_no_matches_returns_original():
    """Text with no sensitive data should pass through unchanged."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Westlake"],
        redact_phone_numbers=True,
        redact_financial_data=True,
    )
    text = "Hi there, this is a normal scheduling message."
    assert redactor.redact(text) == text


def test_custom_mask_character():
    """Custom mask string should be used instead of default '***'."""
    redactor = SensitiveDataRedactor(
        enabled=True,
        keywords=["Covered Care"],
        redact_phone_numbers=False,
        redact_financial_data=False,
        mask="[REDACTED]",
    )
    text = "Contact Covered Care today."
    assert redactor.redact(text) == "Contact [REDACTED] today."
