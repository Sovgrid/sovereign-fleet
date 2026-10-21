"""Tests for fleet.core.catalog provider_notice and end_of_life."""

from fleet.core.catalog import provider_notice, end_of_life


# ── provider_notice ──


def test_provider_notice_none():
    assert provider_notice(None) == {}


def test_provider_notice_empty_dict():
    assert provider_notice({}) == {}


def test_provider_notice_deprecation_date_and_replacement():
    result = provider_notice({
        "deprecation": "2026-11-30T00:00:00Z",
        "deprecation_replacement_model": "m-2",
    })
    assert result == {"retires": "2026-11-30", "replacement": "m-2"}


def test_provider_notice_expiration_date():
    result = provider_notice({"expiration_date": "2027-01-15"})
    assert result == {"retires": "2027-01-15"}


def test_provider_notice_expiration_date_epoch():
    result = provider_notice({"expiration_date": 1800000000})
    assert result == {"retires": "2027-01-15"}


# ── end_of_life ──


def test_end_of_life_with_date():
    result = end_of_life('{"detail": "The model x has reached its end of life on 2026-10-15T08:00:00Z"}')
    assert result == "2026-10-15"


def test_end_of_life_no_date():
    assert end_of_life('{"detail": "The model x has been retired"}') == ""


def test_end_of_life_not_json():
    assert end_of_life("some random text") == ""


def test_end_of_life_empty_body():
    assert end_of_life("") == ""