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
  - FUZZY MATCHING: catches misspellings, AI transcription errors,
    and phonetically-spelled abbreviations (e.g. "YOU SEE F S" → UCFS)

All configuration is driven by environment variables:
  - REDACT_SENSITIVE_DATA:   master on/off switch
  - REDACT_KEYWORDS:         comma-separated extra terms to always mask
  - REDACT_PHONE_NUMBERS:    mask phone numbers in body text
  - REDACT_FINANCIAL_DATA:   broad intelligent detection of financial entities
  - REDACT_FUZZY_MATCH:      catch misspellings & AI transcription variants
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

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

_ENTITY_AFTER_TRIGGER_REGEX = re.compile(
    r"(?:"
    + r"|".join(_FINANCIAL_TRIGGERS)
    + r")"
    + r"\s+"
    + r"((?:[A-Z][a-zA-Z']+(?:\s+(?:of|the|and|&|for)\s+)?(?:\s+[A-Z][a-zA-Z']*)*)+)",
    re.MULTILINE,
)

# Standalone company-name patterns (financial suffixes)
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


# ─────────────────────────────────────────────────────────────────────
# Phonetic letter-spelling map (AI transcription spells out letters)
# "YOU SEE F S" → U C F S → "UCFS"
# ─────────────────────────────────────────────────────────────────────
_LETTER_PHONETICS: dict[str, str] = {
    "ay": "A", "eh": "A",
    "bee": "B", "be": "B",
    "see": "C", "sea": "C", "cee": "C",
    "dee": "D",
    "ee": "E",
    "ef": "F", "eff": "F",
    "gee": "G", "jee": "G",
    "aitch": "H", "ach": "H",
    "eye": "I", "ai": "I",
    "jay": "J",
    "kay": "K",
    "el": "L", "ell": "L",
    "em": "M",
    "en": "N",
    "oh": "O",
    "pee": "P",
    "cue": "Q", "que": "Q", "queue": "Q",
    "ar": "R", "are": "R",
    "es": "S", "ess": "S",
    "tee": "T",
    "you": "U", "yu": "U",
    "vee": "V",
    "double you": "W", "double u": "W",
    "ex": "X",
    "why": "Y", "wy": "Y",
    "zee": "Z", "zed": "Z",
}

# Build regex: match sequences of phonetically-spelled letters
# Sort by length (longest first) so "double you" matches before "you"
_PHONETIC_TOKENS = sorted(_LETTER_PHONETICS.keys(), key=len, reverse=True)
_PHONETIC_TOKEN_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _PHONETIC_TOKENS) + r")\b",
    re.IGNORECASE,
)


def _try_decode_phonetic_spelling(text: str) -> str | None:
    """
    Try to interpret a phrase as phonetically-spelled letters.

    "YOU SEE F S" → "UCFS"
    "dee ee see eye" → "DECI"

    Returns the decoded abbreviation, or None if the phrase doesn't
    look like spelled-out letters.
    """
    words = text.lower().split()
    if len(words) < 2:
        return None

    decoded = []
    for word in words:
        if word in _LETTER_PHONETICS:
            decoded.append(_LETTER_PHONETICS[word])
        elif len(word) == 1 and word.isalpha():
            # Single letter like "F", "S"
            decoded.append(word.upper())
        else:
            # Not a phonetic letter — this isn't a spelled-out abbreviation
            return None

    if len(decoded) >= 2:
        return "".join(decoded)
    return None


def _fuzzy_ratio(a: str, b: str) -> float:
    """Case-insensitive similarity ratio between two strings (0.0 – 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


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
        fuzzy_match:          Catch misspellings & AI transcription variants.
        fuzzy_threshold:      Similarity ratio threshold for fuzzy matching
                               (0.0-1.0). Default 0.72 — catches most typos
                               while avoiding false positives.
        mask:                 The replacement string (default: '***').
    """

    enabled: bool = False
    keywords: list[str] = field(default_factory=list)
    redact_phone_numbers: bool = True
    redact_financial_data: bool = True
    fuzzy_match: bool = True
    fuzzy_threshold: float = 0.72
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
                    "fuzzy_match": self.fuzzy_match,
                    "fuzzy_threshold": self.fuzzy_threshold,
                },
            )

    # ── Fuzzy matching engine ─────────────────────────────────────

    def _fuzzy_redact(self, text: str) -> str:
        """
        Scan the text with sliding windows of varying word-counts
        (matching each keyword's word-count) and replace any window
        whose similarity to a keyword exceeds the fuzzy threshold.

        Also detects phonetically-spelled abbreviations:
          "YOU SEE F S" → decoded as "UCFS" → matches keyword "UCFS"
        """
        if not self.keywords:
            return text

        # Group keywords by word count for efficient window scanning
        kw_by_wordcount: dict[int, list[str]] = {}
        for kw in self.keywords:
            kw_stripped = kw.strip()
            if kw_stripped:
                wc = len(kw_stripped.split())
                kw_by_wordcount.setdefault(wc, []).append(kw_stripped)

        words = text.split()
        replaced_ranges: list[tuple[int, int, str]] = []  # (start_idx, end_idx, mask)

        for wc, kw_list in kw_by_wordcount.items():
            for i in range(len(words) - wc + 1):
                # Skip if this range already has a replacement
                if any(s <= i < e for s, e, _ in replaced_ranges):
                    continue

                window = " ".join(words[i : i + wc])

                for kw in kw_list:
                    ratio = _fuzzy_ratio(window, kw)
                    if ratio >= self.fuzzy_threshold:
                        replaced_ranges.append((i, i + wc, self.mask))
                        break

        # Also check for phonetically-spelled abbreviations
        # Scan windows of 2-8 words (typical letter-spelling is 2-6 letters)
        single_word_keywords = [
            kw.strip().upper()
            for kw in self.keywords
            if kw.strip() and len(kw.strip().split()) == 1
        ]
        if single_word_keywords:
            for window_size in range(2, min(9, len(words) + 1)):
                for i in range(len(words) - window_size + 1):
                    if any(s <= i < e for s, e, _ in replaced_ranges):
                        continue

                    window = " ".join(words[i : i + window_size])
                    decoded = _try_decode_phonetic_spelling(window)

                    if decoded and decoded.upper() in single_word_keywords:
                        replaced_ranges.append(
                            (i, i + window_size, self.mask)
                        )

        if not replaced_ranges:
            return text

        # Sort by position and build the result
        replaced_ranges.sort(key=lambda r: r[0])

        result_words: list[str] = []
        i = 0
        for start, end, mask_str in replaced_ranges:
            # Add non-replaced words before this range
            result_words.extend(words[i:start])
            result_words.append(mask_str)
            i = end
        # Add remaining words
        result_words.extend(words[i:])

        return " ".join(result_words)

    # ── Main redaction entry point ────────────────────────────────

    def redact(self, text: str | None) -> str | None:
        """
        Apply all configured redaction rules to ``text``.

        Processing order (most specific → broadest):
          1. Explicit keywords — exact match (highest priority)
          2. Fuzzy keyword matching — misspellings & AI transcription variants
          3. Financial entity patterns (company names with suffixes)
          4. Contextual entity detection (names after trigger phrases)
          5. Account / loan / reference numbers
          6. SSN and Tax ID patterns
          7. Dollar amounts
          8. Email addresses
          9. Phone numbers

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

        # ── 2. Fuzzy matching (misspellings, AI transcription) ────
        if self.fuzzy_match:
            before_fuzzy = redacted
            redacted = self._fuzzy_redact(redacted)
            if redacted != before_fuzzy:
                categories_hit.append("fuzzy_match")

        # ── 3-8. Financial data patterns ──────────────────────────
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

        # ── 9. Phone numbers ──────────────────────────────────────
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
