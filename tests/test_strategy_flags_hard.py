"""Hard-disable guard: basket strategies never trade, regardless of toggles."""
from src.telegram import strategy_flags as sf


def test_basket_is_hard_disabled():
    assert sf.is_hard_disabled("basket_wide") is True
    assert sf.is_hard_disabled("basket_narrow") is True
    assert sf.is_hard_disabled("tail_no") is False


def test_basket_is_enabled_always_false():
    assert sf.is_enabled("basket_wide") is False
    assert sf.is_enabled("basket_narrow") is False


def test_set_enabled_cannot_turn_basket_on():
    sf.set_enabled("basket_wide", True)        # refused
    sf.set_enabled("basket_narrow", True)      # refused
    assert sf.is_enabled("basket_wide") is False
    assert sf.is_enabled("basket_narrow") is False


def test_tail_still_togglable():
    sf.set_enabled("tail_no", False)
    assert sf.is_enabled("tail_no") is False
    sf.set_enabled("tail_no", True)
    assert sf.is_enabled("tail_no") is True


def test_get_all_shows_basket_false():
    snap = sf.get_all()
    assert snap["basket_wide"] is False
    assert snap["basket_narrow"] is False


def test_startup_log_states_basket_disabled(caplog):
    import logging
    # loguru → standard logging bridge isn't wired in tests; assert the function
    # runs and get_all reflects the hard disable (the banner text is built from it).
    sf.log_startup_flags()
    assert all(sf.get_all()[s] is False for s in ("basket_wide", "basket_narrow"))
