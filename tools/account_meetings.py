"""Offline import of already exported meeting text. No account or network access."""
from datetime import datetime
import hashlib,html,json,math,re,uuid

def _plain(value, depth=0):
    if depth > 12:
        raise ValueError('Meeting text nesting exceeds the supported limit')
    if value is None:
        return ''
    if isinstance(value, str):
        if len(value.encode('utf-8')) > 2_097_152:
            raise ValueError('Meeting text exceeds the supported limit')
        if value.lstrip().startswith(('{', '[')):
            try:
                return _plain(json.loads(value), depth+1)
            except json.JSONDecodeError:
                pass
        if '\n' in value and all(line.lstrip().startswith('{') for line in value.splitlines() if line.strip()):
            try:
                return _plain([json.loads(line) for line in value.splitlines() if line.strip()], depth+1)
            except json.JSONDecodeError:
                pass
        # Ordinary angles are text. Decode only recognizable rich-text blocks;
        # unknown tags stay literal even within those blocks.
        tags = r'p|div|h[1-6]|ul|ol|li|strong|b|em|i|u|span|a|code|pre|blockquote'
        rich = re.search(r'<('+tags+r')(?:\s[^<>]*|)>(?:.|\n)*?</\1\s*>|<br\s*/?>', value, re.I)
        if rich:
            value = re.sub(r'</(?:p|div|h[1-6]|li)>|<br\s*/?>', '\n', value, flags=re.I)
            value = re.sub(r'</?(?:'+tags+r')(?:\s[^<>]*|)\s*/?>', '', value, flags=re.I)
            return html.unescape(value).strip()
        return value.strip()
    if isinstance(value, list):
        return '\n'.join(filter(None, (_plain(item, depth+1) for item in value)))
    if isinstance(value, dict):
        for key in ('text', 'transcript'):
            if isinstance(value.get(key), str):
                return _plain(value[key], depth+1)
        separator = '' if value.get('type') in {'paragraph', 'heading'} else '\n'
        content = value.get('content', [])
        if not isinstance(content, list):
            raise ValueError('Unsupported meeting text')
        return separator.join(filter(None, (_plain(item, depth+1) for item in content)))
    raise ValueError('Unsupported meeting text')

def _created(value):
    if isinstance(value, str) and not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None  # Retain the original; use import time rather than guess a timezone.
        return stamp.timestamp()
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Unsupported meeting timestamp')
    stamp = float(value)
    if not math.isfinite(stamp) or stamp < 0:
        raise ValueError('Unsupported meeting timestamp')
    return stamp/1000 if stamp > 100_000_000_000 else stamp

def import_meetings(store, rows):
    result = {'created': 0, 'unchanged': 0, 'conflicts': 0}
    with store.transaction() as database:
        for row in rows:
            key = 'wispr:cloud-meeting:'+str(uuid.UUID(row['id']))
            fingerprint = hashlib.sha256(json.dumps({key: value for key, value in row.items()
                if key != 'originalText'}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            previous = database.execute('SELECT fingerprint FROM import_records WHERE legacy_key=?', (key,)).fetchone()
            if previous:
                result['unchanged' if previous[0] == fingerprint else 'conflicts'] += 1
                continue
            sid = store._create_session(database, 'meeting', row['title'], row['created'], key, row['notes'])
            if row['transcript']:
                store._add_segment(database, sid, 'legacy', 0, 0, 0, row['transcript'])
            summary = {'items': [{'text': row['summary'], 'kind': 'imported', 'segment_ids': []}]} if row['summary'] else {}
            if row.get('sourceCreatedAt') is not None:
                summary['sourceCreatedAt'] = row['sourceCreatedAt']
                summary['sourceTimezoneUnspecified'] = row['created'] is None
            store._update_session(database, sid, state='complete', summary=summary)
            database.execute('INSERT INTO import_records VALUES(?,?)', (key, fingerprint))
            result['created'] += 1
    return result
