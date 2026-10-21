"""Literal local corrections, separate from speech inference and snippets."""

import re


_LITERALS = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”]*”|‘[^’\n]*’|'
    r"(?<!\w)'[^'\n]*'(?!\w)|https?://[^\s]+|[^\s]+@[^\s]+")
_HESITATIONS = re.compile(r'(?<![\w./@\\\-\'’])(?:[Uu]m{1,3}|[Uu]h{1,3}|[Uu]hm{1,2}|ah{1,3})(?![\w/@\\\-\'’])')

_EMAIL_AT = re.compile(r'\bat(?:[ \t,;:]+(?:sign|symbol))?\b|@', re.IGNORECASE)
_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_DOMAIN_DOT = r'(?:\.(?=[a-z0-9])|[ \t]+\.[ \t]+|[ \t,;:]+(?:dot|period)[ \t,;:]+)'
_EMAIL_DOMAIN = re.compile(_LABEL + '(?:' + _DOMAIN_DOT + _LABEL + r')+(?![\w/@\\-]|\.[\w-])', re.IGNORECASE)
_LOCAL_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_SYMBOL = r'[ \t,;:]+(dot|period|underscore|under[ \t]+score|dash|hyphen|plus)(?:[ \t]+(?:sign|symbol))?[ \t,;:]+'
_LOCAL_TAIL = re.compile(r'(?<![\w./@+%\\-])(' + _LOCAL_ATOM + '(?:(?:' + _LOCAL_SYMBOL +
                         r'|[ \t]*\.[ \t]*|[ \t]+[_+-][ \t]+)' + _LOCAL_ATOM + r'){0,16})$', re.IGNORECASE)
_EMAIL_LEAD = re.compile(r'\b(?:e-?mail(?:[ \t]+address)?(?:[ \t]+is)?|address[ \t]+is|'
                         r'send(?:[ \t]+(?:it|this|that))?[ \t]+to|reply[ \t]+to|contact|'
                         r'use|say|type)[ \t:]+', re.IGNORECASE)
_SYMBOLS = {'dot': '.', 'period': '.', 'underscore': '_', 'under score': '_',
            'dash': '-', 'hyphen': '-', 'plus': '+'}
# These usually introduce a website/location, not an email local part. An exact
# private dictionary replacement can resolve an intentionally ambiguous address.
_LOCATION_WORDS = frozenset('at look meet meeting us me you them it this that here there '
                            'online available located visit website site found more details '
                            'see read work working arrive arriving stay staying back home '
                            'is are was were be i we they and to my email address please the '
                            'can will with from hello thanks information'.split())


def _email_local(raw, *, words):
    value = re.sub(_LOCAL_SYMBOL, lambda m: _SYMBOLS[re.sub(r'[ \t]+', ' ', m[1].lower())],
                   raw, flags=re.IGNORECASE)
    value = re.sub(r'[ \t]*([._+-])[ \t]*', r'\1', value)
    if not words and re.search(r'\s', value):
        return None
    tokens = value.split()
    if not tokens or len(tokens) > 4 or any(t.casefold() in _LOCATION_WORDS for t in tokens):
        return None
    value = ''.join(tokens)
    return value if len(value) <= 64 and re.fullmatch(_LOCAL_ATOM + '(?:\\.' + _LOCAL_ATOM + ')*', value, re.IGNORECASE) else None


def _email_local_before(text, end):
    base = max(0, end - 512)
    left = text[base:end]
    newline = left.rfind('\n') + 1
    base += newline
    left = left[newline:].rstrip(' \t,;:.')
    sentences = list(re.finditer(r'(?<![ \t])[.!?][ \t]+', left))
    if sentences:
        offset = sentences[-1].end()
        base += offset
        left = left[offset:]
    leads = list(_EMAIL_LEAD.finditer(left))
    start = leads[-1].end() if leads else 0
    raw = left[start:]
    local = _email_local(raw, words=True)
    if local and not _EMAIL_AT.search(raw):
        return base + start, local
    # A website location following a verb must not become a person's email.
    if not leads and re.search(r'\b(?:look|meet|visit|work|located|available|learn|read|see|find)\b[^.!?\n]*$', left, re.IGNORECASE):
        return None
    tail = _LOCAL_TAIL.search(left)
    if tail and (local := _email_local(tail[1], words=False)):
        return base + tail.start(1), local
    return None


def format_spoken_emails(text):
    """Normalize spoken address punctuation/symbols without a provider list.

    Keep literal/code/URL spans and common location prose unchanged. This is
    deterministic punctuation formatting, independent of hesitation cleanup.
    """
    literals = [m.span() for m in _LITERALS.finditer(text)]
    parts, cursor = [], 0
    for marker in _EMAIL_AT.finditer(text):
        if marker.start() < cursor:
            continue
        gap = re.match(r'(?:[ \t,;:]|\.(?=[ \t]))*', text[marker.end():])
        right = marker.end() + gap.end()
        domain = _EMAIL_DOMAIN.match(text, right, min(len(text), right + 512))
        if not domain:
            continue
        normalized = re.sub(_DOMAIN_DOT, '.', domain[0], flags=re.IGNORECASE).lower()
        if len(normalized) > 253 or not re.fullmatch('(?:' + _LABEL + r'\.)+[a-z]{2,63}', normalized):
            continue
        local = _email_local_before(text, marker.start())
        if local is None:
            continue
        start, value = local
        if (start < cursor or (start and re.match(r'[\w./@+%\\-]', text[start-1])) or
                any(left < domain.end() and start < end for left, end in literals)):
            continue
        parts.extend([text[cursor:start], value + '@' + normalized])
        cursor = domain.end()
    parts.append(text[cursor:])
    return ''.join(parts)


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
    stripped = format_spoken_emails(stripped)
    return apply_replacements(stripped, profile.get('replacements', {})), text
