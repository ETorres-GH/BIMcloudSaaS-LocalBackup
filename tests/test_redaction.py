import logging

import pytest

from bimcloud_backup.logs import get_logger, setup_logging
from bimcloud_backup.redaction import redact

URL = (
    "https://bimcloud-data.example.invalid/file-manager-service/get-file"
    "?session-id=abc123&file-uri=temp://0000_0&file-name=Casa.BIMProject29&access_token=eyJ.x.y"
)


def test_redact_removes_secret_parameters_and_keeps_the_rest():
    text = redact(f"Falha ao baixar {URL}")
    for secret in ("abc123", "temp://", "eyJ.x.y"):
        assert secret not in text
    assert "session-id=<REMOVIDO>" in text
    assert "access_token=<REMOVIDO>" in text
    assert "file-name=Casa.BIMProject29" in text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer eyJhbGciOi.abc-def_ghi", "eyJhbGciOi"),
        ("download-backup?backup-id=1&resource-id=2&access_token=T0K3N", "T0K3N"),
        ("refresh_token=R3FR3SH&grant_type=refresh_token", "R3FR3SH"),
        ("get-ticket ticket=VElDS0VU", "VElDS0VU"),
        ("oauth2/token?code=C0D3&state=s", "C0D3"),
        ("session_id=S3SS", "S3SS"),
    ],
)
def test_redact_known_secrets(text, secret):
    assert secret not in redact(text)


def test_text_without_secrets_is_unchanged():
    text = "Backup concluído: 3 arquivos em D:/Backups/2026-09-27_230000"
    assert redact(text) == text


def test_log_file_never_contains_tokens(tmp_path):
    setup_logging(verbose=True, directory=tmp_path)
    logger = get_logger()
    try:
        logger.error("Falha em %s", URL)
        try:
            raise OSError(f"conexão recusada: {URL}")
        except OSError:
            logger.exception("Erro inesperado")
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)

    content = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.log"))
    assert "Falha em" in content and "Erro inesperado" in content
    assert "abc123" not in content and "eyJ.x.y" not in content


@pytest.mark.parametrize(
    "text",
    [
        '{"access_token": "T0K3N", "user_id": "u1"}',
        "{'refresh_token': 'T0K3N', 'token_type': 'Bearer'}",
        '{"session-id":"T0K3N"}',
        '{"ticket": "T0K3N\\"x"}',
        "dados={'code': 'T0K3N'}",
    ],
)
def test_redact_json_and_dict_fields(text):
    result = redact(text)
    assert "T0K3N" not in result
    assert "<REMOVIDO>" in result


def test_redact_keeps_other_json_fields():
    text = '{"access_token": "a", "user_id": "u1", "expires_in": 300}'
    assert redact(text) == '{"access_token": "<REMOVIDO>", "user_id": "u1", "expires_in": 300}'
