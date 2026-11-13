"""One shared envelope limit for report generation and the actual transport."""

# PushPlus accepts up to 20,000 characters. Keep a character margin and a
# separate UTF-8 bound; Chinese characters must not be mistaken for bytes.
MAX_CONTENT_CHARS = 18_000
MAX_CONTENT_BYTES = 54_000
