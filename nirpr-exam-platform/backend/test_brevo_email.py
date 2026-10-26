from unittest.mock import MagicMock
import httpx
import pytest
import email_utils


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(email_utils, 'BREVO_API_KEY', 'test-key')
    monkeypatch.setattr(email_utils, 'EMAIL_FROM', 'exams@example.org')
    client = MagicMock()
    monkeypatch.setattr(email_utils.httpx, 'Client', lambda **kw: client)
    smtp = MagicMock(side_effect=AssertionError('SMTP must not be used'))
    monkeypatch.setattr(email_utils.smtplib, 'SMTP', smtp)
    dump = MagicMock()
    monkeypatch.setattr(email_utils, '_dev_dump', dump)
    return client.__enter__(), smtp, dump


def test_brevo_sends_html_and_text_without_smtp(provider):
    client, smtp, dump = provider
    assert email_utils.send_email('candidate@example.org', 'Verify', '<p>Private link</p>', 'Private link')
    call = client.post.call_args
    assert call.args == ('https://api.brevo.com/v3/smtp/email',)
    assert call.kwargs['headers']['api-key'] == 'test-key'
    assert call.kwargs['json']['to'] == [{'email': 'candidate@example.org'}]
    assert call.kwargs['json']['sender']['email'] == 'exams@example.org'
    assert call.kwargs['json']['htmlContent'] == '<p>Private link</p>'
    assert call.kwargs['json']['textContent'] == 'Private link'
    smtp.assert_not_called()
    dump.assert_not_called()


@pytest.mark.parametrize('status', [401, 429, 500])
def test_provider_rejection_does_not_dump_tokens_or_retry(provider, status):
    client, smtp, dump = provider
    response = httpx.Response(status, request=httpx.Request('POST', 'https://api.brevo.com/v3/smtp/email'))
    client.post.return_value = response
    assert not email_utils.send_email('candidate@example.org', 'Reset', 'secret-link')
    client.post.assert_called_once()
    smtp.assert_not_called()
    dump.assert_not_called()


def test_timeout_does_not_retry_or_fall_back(provider):
    client, smtp, dump = provider
    client.post.side_effect = httpx.ReadTimeout('timeout')
    assert not email_utils.send_email('candidate@example.org', 'Reset', 'secret-link')
    client.post.assert_called_once()
    smtp.assert_not_called()
    dump.assert_not_called()


def test_missing_sender_fails_without_sending(provider, monkeypatch):
    client, smtp, dump = provider
    monkeypatch.setattr(email_utils, 'EMAIL_FROM', '')
    assert not email_utils.send_email('candidate@example.org', 'Verify', 'link')
    client.post.assert_not_called()
    dump.assert_not_called()


def test_smtp_still_works_without_brevo(monkeypatch):
    monkeypatch.setattr(email_utils, 'BREVO_API_KEY', '')
    monkeypatch.setattr(email_utils, 'SMTP_HOST', 'smtp.example.org')
    monkeypatch.setattr(email_utils, 'SMTP_USE_TLS', True)
    client = MagicMock()
    monkeypatch.setattr(email_utils.smtplib, 'SMTP', lambda *a, **kw: client)
    assert email_utils.send_email('candidate@example.org', 'Verify', 'link')
    client.__enter__().sendmail.assert_called_once()
