"""
Representative inbound-reply fixtures for Layer 3 extraction tests. Plain data — no
I/O, no live mail/LLM/OCR (the LLM `complete` and PDF `ocr_text` are mocked).

Inputs only: the mock `complete` returns the JSON a real LLM would for these.
"""

# (b) PDF attachment -> OCR text (what the mocked ocr_text returns for the PDF)
PDF_OCR_TEXT = """\
ACME MOTOR SUPPLY -- QUOTATION
Quote #Q-4471   Date: 2026-06-09

Item: Baldor EM3770T  7.5 HP TEFC Motor
Unit Price: $1,210.00 USD
Quantity: 1
Lead time: 5 business days
Terms: FOB Origin, Net 30
"""

# (c) free-text email body carrying a quote
FREE_TEXT_QUOTE = (
    "Thanks for reaching out -- we can supply that. $85 ea, 2 week lead, "
    "minimum order 4 units. Net 30."
)

# (a) structured quote-form submission (clean fields)
FORM_PAYLOAD = {
    "unit_price": 85.0,
    "currency": "USD",
    "quantity": 4,
    "lead_time": "2 weeks",
    "min_order": 4,
    "terms": "Net 30",
}

# junk / no-quote reply (out-of-office) -> must NOT produce a quote
JUNK_REPLY = "Thank you for your email. I am out of office until Monday and will reply then."

# a reply nominating a procurement contact (answers the §3c outbound ask)
NOMINATED_CONTACT_REPLY = (
    "Please send future quote requests to Jane Smith, our Purchasing Manager, "
    "at jane.smith@baypower.com. Thanks!"
)
