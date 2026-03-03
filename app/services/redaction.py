"""
app/services/redaction.py

Intelligent sensitive data redaction engine for SMS messages.

Goes FAR beyond simple keyword matching — automatically detects and masks:
  - Phone numbers (all common US formats)
  - Account / loan / reference / policy numbers
  - Social Security Numbers (SSN) and Tax IDs (EIN)
  - Dollar amounts ($1,234.56)
  - Lender/company names appearing after financial trigger phrases
  - Email addresses
  - Explicitly configured keywords (lender names, company names)

All configuration is driven by environment variables:
  - REDACT_SENSITIVE_DATA:   master on/off switch
  - REDACT_KEYWORDS:         comma-separated extra terms to always mask
  - REDACT_PHONE_NUMBERS:    mask phone numbers in body text
  - REDACT_FINANCIAL_DATA:   broad intelligent detection of financial entities
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Pattern library — each regex targets a specific category of PII/PFI
# ─────────────────────────────────────────────────────────────────────

# US phone numbers: (888) 589-5444, 888-589-5444, 888.589.5444,
#   8885895444, +1 (888) 589-5444, +18885895444, 1-888-589-5444
_PHONE_REGEX = re.compile(
    r"""
    (?<!\d)                     # not preceded by a digit
    (?:\+?1[\s.-]?)?            # optional country code +1 / 1- / 1.
    \(?                         # optional opening paren
    [2-9]\d{2}                  # area code (2-9 start)
    \)?                         # optional closing paren
    [\s.\-]?                    # optional separator
    [2-9]\d{2}                  # exchange (2-9 start)
    [\s.\-]?                    # optional separator
    \d{4}                       # subscriber number
    (?!\d)                      # not followed by a digit
    """,
    re.VERBOSE,
)

# SSN:  123-45-6789  or  123 45 6789
_SSN_REGEX = re.compile(
    r"""
    (?<!\d)
    [0-9]{3}            # area number
    [-\s]               # separator
    [0-9]{2}            # group number
    [-\s]               # separator
    [0-9]{4}            # serial number
    (?!\d)
    """,
    re.VERBOSE,
)

# EIN / Tax ID:  12-3456789
_EIN_REGEX = re.compile(
    r"""
    (?<!\d)
    [0-9]{2}            # prefix
    -                   # dash
    [0-9]{7}            # sequence
    (?!\d)
    """,
    re.VERBOSE,
)

# Dollar amounts:  $1,234.56  $500  $12.34  $1,000,000
_DOLLAR_REGEX = re.compile(
    r"""
    \$\s?               # dollar sign with optional space
    \d{1,3}             # leading digits
    (?:,\d{3})*         # optional comma-separated thousands
    (?:\.\d{1,2})?      # optional cents
    """,
    re.VERBOSE,
)

# Account / Loan / Reference / Policy / Case numbers
# Matches patterns like:
#   account #12345, account number: 12345, acct 12345-6789
#   loan #12345, loan number 12345
#   reference #ABC-123, ref: 12345, ref# 12345
#   policy #12345, policy number 12345
#   case #12345, case number: 12345
_ACCOUNT_NUM_REGEX = re.compile(
    r"""
    (?:account|acct|loan|reference|ref|policy|case|contract|invoice|claim)
    \s*
    (?:
        (?:number|num|no|id)            # label present (acts as separator)
        [\s:\#.\-]*                     # optional extra separator
      |
        [\#:\.\-]\s*                    # OR explicit separator required
    )
    ([A-Za-z0-9][\w\-]{2,20})          # the actual number/ID (captured)
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Email addresses
_EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
)

# ── Contextual financial entity detection ────────────────────────
# Captures company/lender names that appear after trigger phrases.
# Examples:
#   "managed by Westlake Portfolio Management"
#   "financed through Capital One Auto Finance"
#   "serviced by Covered Care"
#   "contact Covered Care (Westlake Portfolio Management)"
#   "lender: Westlake Portfolio Management"
#   "creditor is ABC Financial Services"

# Trigger phrases that precede a lender/company name
_FINANCIAL_TRIGGERS = [
    r"managed\s+by",
    r"financed?\s+(?:by|through|via|with)",
    r"serviced\s+by",
    r"owned\s+by",
    r"assigned\s+to",
    r"transferred\s+to",
    r"sold\s+to",
    r"handled\s+by",
    r"held\s+(?:by|with|at)",
    r"lender(?:\s+is|\s*[:\-])",
    r"creditor(?:\s+is|\s*[:\-])",
    r"servicer(?:\s+is|\s*[:\-])",
    r"portfolio(?:\s+of|\s*[:\-])",
    r"(?:please\s+)?contact",
    r"(?:call|reach(?:\s+out\s+to)?|notify)",
    r"account\s+with",
    r"(?:loan|debt|balance)\s+(?:with|from|through|at)",
]

# Build a single regex:  (trigger phrase)\s+(Capitalized Name spanning 1-6 words)
_ENTITY_AFTER_TRIGGER_REGEX = re.compile(
    r"(?:"
    + r"|".join(_FINANCIAL_TRIGGERS)
    + r")"
    + r"\s+"
    + r"((?:[A-Z][a-zA-Z']+(?:\s+(?:of|the|and|&|for)\s+)?(?:\s+[A-Z][a-zA-Z']*)*)+)",
    re.MULTILINE,
)

# Standalone company-name patterns (multi-word capitalized names ending
# with common financial suffixes)
_FINANCIAL_COMPANY_REGEX = re.compile(
    r"""
    \b
    (
        (?:[A-Z][a-zA-Z']+\s+){0,5}   # up to 5 capitalized words
        (?:
            Portfolio\s+Management
            | Financial\s+(?:Services|Group|Corp(?:oration)?|Solutions)
            | Auto\s+Finance
            | Loan\s+(?:Services|Servicing)
            | Capital\s+(?:Group|Management|Partners)
            | Credit\s+(?:Union|Corp(?:oration)?|Services)
            | Lending(?:\s+(?:LLC|Inc|Corp))?
            | Mortgage(?:\s+(?:LLC|Inc|Corp|Services))?
            | Collections?(?:\s+(?:Agency|Services|LLC|Inc))?
            | Debt\s+(?:Solutions|Recovery|Services)
        )
    )
    \b
    """,
    re.VERBOSE,
)


@dataclass
class SensitiveDataRedactor:
    """
    Intelligent, configurable text redactor.

    Attributes:
        enabled:              Master switch. If False, redact() is a no-op.
        keywords:             List of case-insensitive terms to always replace.
        redact_phone_numbers: Whether to mask phone numbers detected via regex.
        redact_financial_data: Broad detection of account numbers, SSN, dollar
                               amounts, lender names in context, emails, etc.
        mask:                 The replacement string (default: '***').
    """

    enabled: bool = False
    keywords: list[str] = field(default_factory=list)
    redact_phone_numbers: bool = True
    redact_financial_data: bool = True
    mask: str = "***"

    # Pre-compiled regex patterns for each keyword (built on first use)
    _keyword_patterns: list[re.Pattern] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Compile keyword patterns once at construction time."""
        self._keyword_patterns = []
        for kw in self.keywords:
            kw_stripped = kw.strip()
            if kw_stripped:
                pattern = re.compile(re.escape(kw_stripped), re.IGNORECASE)
                self._keyword_patterns.append(pattern)

        if self.enabled:
            logger.info(
                "Sensitive data redactor enabled",
                extra={
                    "event": "redactor_enabled",
                    "keyword_count": len(self._keyword_patterns),
                    "redact_phone_numbers": self.redact_phone_numbers,
                    "redact_financial_data": self.redact_financial_data,
                },
            )

    def redact(self, text: str | None) -> str | None:
        """
        Apply all configured redaction rules to ``text``.

        Processing order (most specific → broadest):
          1. Explicit keywords (highest priority — always applied)
          2. Financial entity patterns (company names with suffixes)
          3. Contextual entity detection (names after trigger phrases)
          4. Account / loan / reference numbers
          5. SSN and Tax ID patterns
          6. Dollar amounts
          7. Email addresses
          8. Phone numbers

        Returns the redacted string, or the original if redaction is disabled
        or the text is empty/None.
        """
        if not self.enabled or not text:
            return text

        original = text
        redacted = text
        categories_hit: list[str] = []

        # ── 1. Explicit keywords (always applied) ─────────────────
        for pattern in self._keyword_patterns:
            if pattern.search(redacted):
                redacted = pattern.sub(self.mask, redacted)
                categories_hit.append("keyword")

        # ── 2-7. Financial data patterns ──────────────────────────
        if self.redact_financial_data:
            # Company names with financial suffixes
            if _FINANCIAL_COMPANY_REGEX.search(redacted):
                redacted = _FINANCIAL_COMPANY_REGEX.sub(self.mask, redacted)
                categories_hit.append("financial_company")

            # Contextual: entity names after trigger phrases
            if _ENTITY_AFTER_TRIGGER_REGEX.search(redacted):
                redacted = _ENTITY_AFTER_TRIGGER_REGEX.sub(
                    lambda m: m.group(0).replace(m.group(1), self.mask),
                    redacted,
                )
                categories_hit.append("contextual_entity")

            # Account/loan/reference numbers
            if _ACCOUNT_NUM_REGEX.search(redacted):
                redacted = _ACCOUNT_NUM_REGEX.sub(
                    lambda m: m.group(0).replace(m.group(1), self.mask),
                    redacted,
                )
                categories_hit.append("account_number")

            # SSN
            if _SSN_REGEX.search(redacted):
                redacted = _SSN_REGEX.sub(self.mask, redacted)
                categories_hit.append("ssn")

            # EIN / Tax ID
            if _EIN_REGEX.search(redacted):
                redacted = _EIN_REGEX.sub(self.mask, redacted)
                categories_hit.append("ein")

            # Dollar amounts
            if _DOLLAR_REGEX.search(redacted):
                redacted = _DOLLAR_REGEX.sub(self.mask, redacted)
                categories_hit.append("dollar_amount")

            # Email addresses
            if _EMAIL_REGEX.search(redacted):
                redacted = _EMAIL_REGEX.sub(self.mask, redacted)
                categories_hit.append("email")

        # ── 8. Phone numbers ──────────────────────────────────────
        if self.redact_phone_numbers:
            if _PHONE_REGEX.search(redacted):
                redacted = _PHONE_REGEX.sub(self.mask, redacted)
                categories_hit.append("phone_number")

        # ── Log if anything changed ───────────────────────────────
        if redacted != original:
            logger.info(
                "Sensitive data redacted from message text",
                extra={
                    "event": "sensitive_data_redacted",
                    "categories": categories_hit,
                    "original_length": len(original),
                    "redacted_length": len(redacted),
                },
            )

        return redacted
