"""Literal local corrections, separate from speech inference and snippets."""

import re


_LITERALS = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”]*”|‘[^’\n]*’|'
    r"(?<!\w)'[^'\n]*'(?!\w)|https?://[^\s]+|[^\s]+@[^\s]+")
_HESITATIONS = re.compile(r'(?<![\w./@\\\-\'’])(?:[Uu]m{1,3}|[Uu]h{1,3}|[Uu]hm{1,2}|ah{1,3})(?![\w/@\\\-\'’])')


def clean_hesitations(text):
    """Remove a small set of hesitations, without paraphrasing or reflowing text.

    Quoted text, code, addresses, compounds and uppercase abbreviations are literal.
    Lowercase comma-delimited 'ah' is eligible; the meaningful interjection 'Ah'
    is retained. Only whitespace/punctuation touching a removed token is repaired.
    """
    literals = [(m.start(), m.end()) for m in _LITERALS.finditer(text)]
    matches = list(_HESITATIONS.finditer(text))
    for match in reversed(matches):
        start, end = match.span()
        if any(left <= start < right for left, right in literals):
            continue
        if re.match(r'\.\w', text[end:]):
            continue  # A filename or domain, rather than a sentence-ending stop.
        token = match.group()
        if token.startswith('a') and not (re.search(r',[ \t]*$', text[:start])
                                         and re.match(r'[ \t]*,[ \t]*\w', text[end:])):
            continue
        left = start
        while left and text[left - 1] in ' \t':
            left -= 1
        paired_comma = left > 0 and text[left - 1] == ','
        if paired_comma:
            start = left - 1
        trailing = re.match(r'[ \t]*(?:,[ \t]*)?', text[end:]).group()
        end += len(trailing)
        prefix = text[:start]
        sentence_start = not prefix.strip() or bool(re.search(r'[.!?]\s*$', prefix))
        if sentence_start and end < len(text) and text[end] == '.':
            end += 1
            while end < len(text) and text[end] in ' \t':
                end += 1
        suffix = text[end:]
        if sentence_start and token[0].isupper():
            word = re.match(r'[^\W\d_]+', suffix)
            if word and word.group().islower():
                suffix = suffix[0].upper() + suffix[1:]
        # A paired pause comma takes its surrounding spaces with it. Retain one
        # separator if speech follows, and no stray space before punctuation.
        if paired_comma and suffix and not suffix[0].isspace() and suffix[0] not in '.,;:!?':
            prefix += ' '
        elif suffix and suffix[0] in '.,;:!?':
            prefix = prefix.rstrip(' \t')
        text = prefix + suffix
    return text


def apply_replacements(text, replacements):
    """Replace literal whole tokens once, longest match first, without cascading."""
    entries = sorted(((key, value) for key, value in replacements.items() if key),
                     key=lambda item: (-len(item[0]), item[0].casefold(), item[0]))
    if not entries:
        return text
    groups = '|'.join(f'(?P<r{i}>{re.escape(key)})' for i, (key, _) in enumerate(entries))
    expression = re.compile(r'(?<!\w)(?:' + groups + r')(?!\w)', re.IGNORECASE)
    return expression.sub(lambda match: entries[int(match.lastgroup[1:])][1], text)


def apply_personalization(text, profile, *, clean_fillers=False):
    """Return (processed, original) for undo. Snippets require the entire trigger.

    Cleanup is opt-in at this boundary. Expanded snippets are not cleaned or
    corrected again; custom words remain inference hints.
    """
    stripped = text.strip()
    for trigger, expansion in profile.get('snippets', {}).items():
        if trigger and stripped.casefold() == trigger.casefold():
            return expansion, text
    if clean_fillers:
        stripped = clean_hesitations(stripped).strip()
    return apply_replacements(stripped, profile.get('replacements', {})), text
