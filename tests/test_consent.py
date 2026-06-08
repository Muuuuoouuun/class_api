import pytest

from classin_toolkit.intelligence.consent import (
    ConsentStatus, ConsentError, ensure_can_analyze, can_add_to_corpus,
)


def test_no_consent_blocks_analysis():
    with pytest.raises(ConsentError):
        ensure_can_analyze(ConsentStatus.NONE)


def test_internal_consent_allows_analysis_but_not_corpus():
    ensure_can_analyze(ConsentStatus.INTERNAL)        # raise 안 함
    assert can_add_to_corpus(ConsentStatus.INTERNAL) is False


def test_corpus_consent_allows_both():
    ensure_can_analyze(ConsentStatus.CORPUS)
    assert can_add_to_corpus(ConsentStatus.CORPUS) is True


def test_from_label_maps_korean():
    assert ConsentStatus.from_label("코퍼스활용") is ConsentStatus.CORPUS
    assert ConsentStatus.from_label("미동의") is ConsentStatus.NONE
