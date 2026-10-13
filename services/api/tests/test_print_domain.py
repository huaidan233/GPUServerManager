from pydantic import ValidationError
import pytest

from gsm_api.print_domain import (
    PrintSettings,
    canonical_page_ranges,
    ensure_transition,
)


def test_print_settings_apply_confirmed_limits():
    value = PrintSettings(
        printer_id="printer-a",
        copies=20,
        page_ranges="3-5,1,5-6",
        sides="duplex_long_edge",
        color_mode="monochrome",
        paper="A4",
    )

    assert canonical_page_ranges(value.page_ranges, page_count=10) == "1,3-6"
    with pytest.raises(ValidationError):
        PrintSettings(printer_id="printer-a", copies=21)


@pytest.mark.parametrize(
    "value",
    ["0", "4-2", "1,,2", "page 1", "11", "1-11"],
)
def test_page_ranges_reject_invalid_or_out_of_bounds_values(value):
    with pytest.raises(ValueError):
        canonical_page_ranges(value, page_count=10)


def test_blank_page_ranges_select_all_pages():
    assert canonical_page_ranges("", page_count=4) == "1-4"


def test_spooling_cannot_return_to_queue_or_be_automatically_released():
    ensure_transition("leased", "queued")
    ensure_transition("leased", "spooling")

    with pytest.raises(ValueError):
        ensure_transition("spooling", "queued")


def test_terminal_states_cannot_transition():
    for status in ("completed", "failed", "cancelled", "delivery_unknown"):
        with pytest.raises(ValueError):
            ensure_transition(status, "queued")
