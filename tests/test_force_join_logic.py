from bot.handlers.force_join import is_required_member


def test_regular_membership_statuses_are_accepted():
    assert is_required_member("member")
    assert is_required_member("administrator")
    assert is_required_member("creator")


def test_restricted_user_requires_is_member_true():
    assert is_required_member("restricted", True)
    assert not is_required_member("restricted", False)
    assert not is_required_member("left")
    assert not is_required_member("kicked")
