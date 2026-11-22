"""Integration tests for summary composition and conditional note writes."""

from conftest import load_backend_submodule
from fake_api import FakeApi

bridge = load_backend_submodule('summary_bridge')
note_merge = load_backend_submodule('core.note_merge')


class MemoryStore:
    def __init__(self):
        self.metadata = {}

    def update_card_meta(self, key, patch):
        self.metadata[str(key)] = dict(patch)
        return True


def test_write_summary_recomposes_after_concurrent_note_edit(tmp_path):
    api = FakeApi(data_dir=str(tmp_path))
    store = MemoryStore()
    api._notes['card.json'] = '原有备注'
    original_write = api.cards.write_note
    calls = []

    def write_with_one_race(card_id, text, *, expected_text=None):
        calls.append((text, expected_text))
        if len(calls) == 1:
            api._notes[card_id] = '用户刚补充的备注'
        return original_write(card_id, text, expected_text=expected_text)

    api.cards.write_note = write_with_one_race

    result = bridge.write_summary(api, store, 'card.json', 'AI 简介')

    assert result['ok'] is True
    assert result['origin'] == bridge.ORIGIN_CARD_NOTE
    assert len(calls) == 2
    assert calls[0][1] == '原有备注'
    assert calls[1][1] == '用户刚补充的备注'
    assert '用户刚补充的备注' in api._notes['card.json']
    assert 'AI 简介' in api._notes['card.json']
    assert note_merge.NOTE_BLOCK_START in api._notes['card.json']


def test_write_summary_does_not_use_unsafe_legacy_write_signature(tmp_path):
    api = FakeApi(data_dir=str(tmp_path))
    store = MemoryStore()
    api._notes['card.json'] = '用户备注'
    legacy_calls = []

    def legacy_write(card_id, text):
        legacy_calls.append((card_id, text))
        api._notes[card_id] = text
        return True

    api.cards.write_note = legacy_write

    result = bridge.write_summary(api, store, 'card.json', 'AI 简介')

    assert result['ok'] is False
    assert result['origin'] == bridge.ORIGIN_NAMESPACE
    assert '条件备注写入' in result['message']
    assert legacy_calls == []
    assert api._notes['card.json'] == '用户备注'
    assert store.metadata['card.json']['summary_origin'] == bridge.ORIGIN_NAMESPACE
    assert '用户备注' in store.metadata['card.json']['summary']
    assert 'AI 简介' in store.metadata['card.json']['summary']


def test_write_summary_stops_retrying_when_write_fails_without_conflict(tmp_path):
    api = FakeApi(data_dir=str(tmp_path))
    store = MemoryStore()
    api._notes['card.json'] = '用户备注'
    calls = []

    def reject_write(card_id, text, *, expected_text=None):
        calls.append((card_id, text, expected_text))
        return False

    api.cards.write_note = reject_write

    result = bridge.write_summary(api, store, 'card.json', 'AI 简介')

    assert result['ok'] is False
    assert len(calls) == 1
    assert result['origin'] == bridge.ORIGIN_NAMESPACE


def test_write_summary_bounds_retries_and_degrades_latest_composition(tmp_path):
    api = FakeApi(data_dir=str(tmp_path))
    store = MemoryStore()
    api._notes['card.json'] = '初始备注'
    original_write = api.cards.write_note
    calls = []

    def write_with_repeated_race(card_id, text, *, expected_text=None):
        calls.append((text, expected_text))
        api._notes[card_id] = f'并发备注{len(calls)}'
        return original_write(card_id, text, expected_text=expected_text)

    api.cards.write_note = write_with_repeated_race

    result = bridge.write_summary(api, store, 'card.json', 'AI 简介')

    assert result['ok'] is False
    assert result['origin'] == bridge.ORIGIN_NAMESPACE
    assert len(calls) == bridge.MAX_NOTE_WRITE_ATTEMPTS
    assert f'并发备注{len(calls)}' in store.metadata['card.json']['summary']
    assert 'AI 简介' in store.metadata['card.json']['summary']
