from bot.handlers.owner_intelligence import (
    is_24h_report_request,
    is_audit_request,
)


def test_audit_request_roman_urdu():
    assert is_audit_request("Alexa bot ko check karo konsa feature working nahi hai")


def test_audit_request_english():
    assert is_audit_request("Run a complete feature health audit")


def test_24h_report_roman_urdu():
    assert is_24h_report_request("Alexa pichle 24 ghante ki bot report do")


def test_24h_report_english():
    assert is_24h_report_request("Give me the last 24 hours activity report")


def test_regular_chat_is_not_intercepted():
    assert not is_audit_request("Alexa how are you")
    assert not is_24h_report_request("Tell me a joke")
