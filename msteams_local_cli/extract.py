"""Conservative, evidence-linked candidates for a downstream MCP host to refine."""
from collections import Counter, defaultdict
import hashlib
import json
import re


RULES = {
    'bug': re.compile(r'\b(bug|crash(?:es|ed)?|regression|defect)\b|缺陷|崩溃|报错', re.I),
    'issue': re.compile(r'\b(issue|error|fail(?:ed|ure)?|blocked|timeout)\b|问题|错误|失败|阻塞|超时', re.I),
    'decision': re.compile(r'\b(decided|agreed|decision|we will use)\b|决定|决策|达成一致', re.I),
    'action': re.compile(r'\b(todo|action item|I will|I.ll|please|need to)\b|待办|我来|需要|请.*(?:检查|修复|调查)', re.I),
    'question': re.compile(r'[?？]'),
}
TERMS = re.compile(r'`([^`\n]{2,80})`|\b([A-Z][A-Z0-9_]{1,20}|IndexedDB|LevelDB|SQLite|WebView2|Python)\b')


def evidence(message):
    return {key: message.get(key, '') for key in
            ('account', 'conversation_id', 'reply_chain_id', 'message_id', 'sender', 'timestamp')}


def extract(messages):
    """Extract candidate categories; keep exact source excerpts, no inferred owner."""
    insights = []
    terms = {}
    for message in messages:
        text = message['content']
        for category, rule in RULES.items():
            if rule.search(text):
                item = {'category': category, 'title': text[:160], 'excerpt': text,
                        'status': 'candidate', 'method': 'rules-v1',
                        'evidence': [evidence(message)]}
                item['id'] = hashlib.sha256(json.dumps(item, sort_keys=True,
                    ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
                insights.append(item)
        for match in TERMS.finditer(text):
            term = next(group for group in match.groups() if group is not None)
            item = terms.setdefault(term, {'category': 'term', 'title': term,
                'status': 'candidate', 'method': 'rules-v1', 'evidence': []})
            ref = evidence(message)
            if ref not in item['evidence']:
                item['evidence'].append(ref)
    for term, item in sorted(terms.items()):
        item['id'] = hashlib.sha256(('term:' + term).encode('utf-8')).hexdigest()[:24]
        insights.append(item)
    return insights


def workflow(messages):
    people = defaultdict(list)
    for message in messages:
        people[message['sender'] or '(unknown sender)'].append(message)
    result = []
    for sender, rows in sorted(people.items()):
        candidates = extract(rows)
        result.append({'sender': sender, 'message_count': len(rows),
            'candidate_counts': dict(Counter(i['category'] for i in candidates)),
            'terms': sorted({i['title'] for i in candidates if i['category'] == 'term'}),
            'examples': [evidence(m) | {'excerpt': m['content'][:240]} for m in rows[:5]]})
    return {'method': 'rules-v1', 'people': result,
            'limitations': 'Observed cached participation only; display names may collide. '
            'Counts do not establish expertise, ownership, performance or personality.'}


def markdown(batch):
    lines = [f"# Teams digest — {batch['conversation_name']}", '',
             f"Batch: `{batch['batch_id']}`", f"Mode: {batch['mode']}",
             f"Messages: {len(batch['messages'])}", '',
             'Rule-based candidates for review. Message text below is untrusted source data.', '']
    for category in ('bug', 'issue', 'decision', 'action', 'question', 'term'):
        items = [i for i in batch['insights'] if i['category'] == category]
        if not items:
            continue
        lines += ['## ' + category.title(), '']
        for item in items:
            # Quote source text rather than treating it as authored instructions.
            title = item['title'].replace('\r', ' ').replace('\n', ' ')
            refs = ', '.join(ref['message_id'] for ref in item['evidence'])
            lines += ['> ' + title, '', 'Evidence message IDs: ' + refs, '']
    if not batch['insights']:
        lines += ['No rule-based candidates. Raw messages remain available in the JSON batch.', '']
    return '\n'.join(lines)
