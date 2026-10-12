import importlib
import importlib.util
import json
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from rag.corpus import load_corpus


def policy_module():
    assert importlib.util.find_spec('rag.policy'), '政策元数据校验尚未实现'
    return importlib.import_module('rag.policy')


def metadata_record(text='# 政策\n\n## 规则\n\n正文。\n'):
    return {
        'source': 'a.md', 'policy_id': 'order', 'version': 'v1',
        'effective_from': '2026-10-11T16:00:00Z', 'effective_until': None,
        'source_sha256': sha256(text.encode('utf-8')).hexdigest(),
        'rules': {'order.cancel': {'titles': ['规则'], 'values': ['待付款', '待发货']}},
    }


def test_real_corpus_has_versioned_metadata():
    chunks = load_corpus()
    assert len(chunks) == 18
    assert all(getattr(c, 'metadata', None) is not None for c in chunks)
    metadata = next(c.metadata for c in chunks if c.source == 'order-policy.md')
    assert metadata.rules['order.cancel'].values == ['待付款', '待发货']
    assert metadata.active_at(datetime(2026, 10, 12, 12, tzinfo=UTC))


def test_policy_digest_normalizes_newlines():
    digest = policy_module().policy_digest
    assert digest('政策\r\n正文\r\n') == sha256('政策\n正文\n'.encode()).hexdigest()
    assert digest('政策\r正文\r') == digest('政策\n正文\n')


@pytest.mark.parametrize(('at', 'expected'), [
    ('2026-10-11T15:59:59Z', False), ('2026-10-11T16:00:00Z', True),
    ('2026-10-12T11:59:59Z', True), ('2026-10-12T12:00:00Z', False),
])
def test_effective_interval_includes_start_excludes_end(at, expected):
    record = metadata_record()
    record['effective_until'] = '2026-10-12T12:00:00Z'
    metadata = policy_module().PolicyMetadata.model_validate(record)
    assert metadata.active_at(datetime.fromisoformat(at)) is expected


@pytest.mark.parametrize(('field', 'value'), [
    ('effective_from', '2026-10-12T00:00:00'), ('effective_from', True),
    ('effective_until', '2026-10-11T15:00:00Z'),
    ('effective_until', '2026-10-11T16:00:00Z'), ('effective_until', False),
    ('source', '../a.md'), ('source', 'C:\\a.md'), ('source', 'https://x/a.md'),
    ('source', 'a\\b.md'), ('source', '.md'), ('version', ' '),
    ('source_sha256', 'not-a-hash'),
])
def test_invalid_metadata_is_rejected(field, value):
    record = metadata_record()
    record[field] = value
    with pytest.raises(ValueError):
        policy_module().PolicyMetadata.model_validate(record)


def test_naive_query_time_is_rejected():
    metadata = policy_module().PolicyMetadata.model_validate(metadata_record())
    with pytest.raises(ValueError, match='时区'):
        metadata.active_at(datetime(2026, 10, 12))


@pytest.mark.parametrize('values', [[], [' '], ['待付款', '待付款'], [True]])
def test_rule_values_are_nonempty_unique_strings(values):
    record = metadata_record()
    record['rules']['order.cancel']['values'] = values
    with pytest.raises(ValueError):
        policy_module().PolicyMetadata.model_validate(record)


@pytest.mark.parametrize('change', ['missing', 'digest', 'title', 'unsafe', 'extra', 'duplicate'])
def test_corpus_rejects_missing_or_inconsistent_catalog(tmp_path, change):
    text = '# 政策\n\n## 规则\n\n正文。\n'
    (tmp_path / 'a.md').write_text(text, encoding='utf-8')
    record = metadata_record(text)
    documents = [record]
    if change == 'missing':
        documents = []
    elif change == 'digest':
        record['source_sha256'] = '0' * 64
    elif change == 'title':
        record['rules']['order.cancel']['titles'] = ['不存在的小节']
    elif change == 'unsafe':
        record['source'] = '../a.md'
    elif change == 'extra':
        (tmp_path / 'unlisted.md').write_text(text, encoding='utf-8')
    else:
        documents.append(dict(record))
    (tmp_path / 'catalog.json').write_text(json.dumps({
        'schema_version': 1, 'documents': documents,
    }), encoding='utf-8')
    with pytest.raises(ValueError):
        load_corpus(tmp_path)
