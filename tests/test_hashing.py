"""内容哈希：稳定性与兼容性（幂等判定的地基）。"""

from stm_ai_core import hashing

CARD = {
    'name': 'Alice',
    'description': '一名骑士',
    'first_mes': '你好，旅行者。',
    'personality': '沉稳、寡言',
    'scenario': '北境城堡',
    'mes_example': '<START>\n{{user}}: 你好',
    'character_book': {
        'entries': [
            {'comment': '世界观', 'content': '北境常年积雪'},
            {'comment': '魔法体系', 'content': '元素魔法'},
        ],
    },
}


def test_hash_is_stable_across_calls():
    assert hashing.card_content_hash(CARD) == hashing.card_content_hash(dict(CARD))


def test_hash_ignores_dict_key_order():
    reordered = {
        'mes_example': CARD['mes_example'],
        'scenario': CARD['scenario'],
        'personality': CARD['personality'],
        'first_mes': CARD['first_mes'],
        'description': CARD['description'],
        'name': CARD['name'],
        'character_book': CARD['character_book'],
    }
    assert hashing.card_content_hash(reordered) == hashing.card_content_hash(CARD)


def test_hash_ignores_worldbook_entry_order():
    shuffled = dict(CARD)
    shuffled['character_book'] = {
        'entries': list(reversed(CARD['character_book']['entries'])),
    }
    assert hashing.card_content_hash(shuffled) == hashing.card_content_hash(CARD)


def test_hash_changes_when_content_changes():
    modified = dict(CARD, description='一名法师')
    assert hashing.card_content_hash(modified) != hashing.card_content_hash(CARD)


def test_hash_changes_when_worldbook_entry_added():
    modified = dict(CARD)
    modified['character_book'] = {
        'entries': CARD['character_book']['entries'] + [{'comment': '地理', 'content': '…'}],
    }
    assert hashing.card_content_hash(modified) != hashing.card_content_hash(CARD)


def test_hash_normalizes_newlines_and_whitespace():
    left = {'name': 'Alice', 'description': 'a\r\nb'}
    right = {'name': 'Alice', 'description': 'a\nb'}
    padded = {'name': 'Alice', 'description': '  a\nb  '}
    assert hashing.card_content_hash(left) == hashing.card_content_hash(right)
    assert hashing.card_content_hash(padded) == hashing.card_content_hash(right)


def test_hash_supports_v3_data_block():
    flat = {'name': 'Alice', 'description': '骑士'}
    nested = {'name': 'Alice', 'data': {'name': 'Alice', 'description': '骑士'}}
    assert hashing.card_content_hash(flat) == hashing.card_content_hash(nested)


def test_hash_accepts_v2_style_worldbook_list():
    with_list = dict(CARD)
    with_list['character_book'] = CARD['character_book']['entries']
    with_dict = dict(CARD)
    with_dict['character_book'] = {'entries': CARD['character_book']['entries']}
    assert hashing.card_content_hash(with_list) == hashing.card_content_hash(with_dict)


def test_hash_handles_empty_card():
    digest = hashing.card_content_hash({})
    assert digest.startswith(hashing.HASH_PREFIX)
    assert len(digest) == len(hashing.HASH_PREFIX) + 64


def test_extra_worldbook_names_participate():
    base = hashing.card_content_hash(CARD)
    extra = hashing.card_content_hash(CARD, ['外置条目'])
    assert base != extra


def test_short_hash_strips_prefix():
    digest = hashing.card_content_hash(CARD)
    assert hashing.short_hash(digest, 12) == digest[len(hashing.HASH_PREFIX):][:12]
    assert len(hashing.short_hash(digest, 4)) == 4
