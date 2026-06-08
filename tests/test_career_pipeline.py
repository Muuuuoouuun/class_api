import pytest

from classin_toolkit.intelligence.consent import ConsentError, ConsentStatus
from classin_toolkit.pipelines.career import guard_consent


def test_pipeline_blocks_before_any_claude_call():
    with pytest.raises(ConsentError):
        guard_consent(ConsentStatus.NONE)


def test_pipeline_allows_internal():
    assert guard_consent(ConsentStatus.INTERNAL) is None
