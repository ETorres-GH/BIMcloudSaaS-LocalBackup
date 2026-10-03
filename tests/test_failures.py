import pytest

from bimcloud_backup.failures import (
    KIND_FOLDER,
    KIND_ITEM,
    KIND_LIBRARY,
    Failure,
    count_failed,
    explain,
    reason,
    where,
    why,
)

# The failure seen in a real test, with the resource id made up.
SERVER_JOB_FAILED = (
    "A exportação terminou com status 'failed': ModelServerSideError: Model server job failed "
    "with code 18, message: Failed to create archive of resource: "
    "'00000000-0000-0000-0000-000000000000'..\n"
    "    at async (webpack:///modules/exemplo/ModelServerJobHandle.js:10:5)"
)


def test_a_failed_server_job_says_what_to_try():
    failure = Failure("Project Root/Libraries/BibliotecaB.pla", KIND_LIBRARY, SERVER_JOB_FAILED)
    assert explain(failure) == (
        "Biblioteca Project Root/Libraries/BibliotecaB.pla: o BIMcloud não conseguiu gerar o "
        "arquivo (erro 18 do servidor). Tente exportar pelo BIMcloud Manager; se falhar lá "
        "também, é um problema no servidor."
    )
    assert where(failure) == "Biblioteca Project Root/Libraries/BibliotecaB.pla"
    assert why(failure).startswith("O BIMcloud não conseguiu gerar o arquivo (erro 18")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("A exportação terminou com status 'aborted'", "não conseguiu gerar o arquivo. Tente"),
        ("[Errno 28] No space left on device", "faltou espaço no disco"),
        ("[WinError 112] Não há espaço suficiente no disco", "faltou espaço no disco"),
        ("A exportação não terminou em 60 minutos", "levou mais de 60 minutos"),
        ("Falha ao baixar a.pdf: Read timed out. (read timeout=60)", "demorou demais"),
        ("pasta de origem não encontrada no BIMcloud", "não existe mais no BIMcloud"),
        ("HTTP 404 Not Found", "não existe mais no BIMcloud"),
        ("invalid_grant: refresh token expired", "o acesso ao BIMcloud expirou"),
        ("Falha de conexão com o BIMcloud: Connection aborted.", "a conexão com o BIMcloud caiu"),
        ("Download incompleto: 10 de 20 bytes", "a conexão com o BIMcloud caiu"),
    ],
)
def test_common_failures_in_plain_words(message, expected):
    assert expected in reason(message)


def test_other_failures_keep_the_original_message_without_the_stack_trace():
    failure = Failure("Project Root/Obras/a.pdf", "file", "Algo inesperado\n  at linha 3")
    assert reason(failure.message) is None
    assert explain(failure) == "Arquivo Project Root/Obras/a.pdf: Algo inesperado"


def test_counts_agree_in_number():
    assert count_failed(1) == "1 item falhou"
    assert count_failed(3) == "3 itens falharam"


def test_round_trip_and_older_texts():
    failure = Failure("Project Root/Obras", KIND_FOLDER, "pasta de origem não encontrada")
    assert Failure.from_dict(failure.to_dict()) == failure
    assert Failure.from_dict({**failure.to_dict(), "kind": "outro"}).kind == KIND_ITEM
    with pytest.raises(TypeError):
        Failure.from_dict({"path": "x"})
    older = Failure.from_text("Project Root/Obras/a.pdf: sem conexão")
    assert older == Failure("Project Root/Obras/a.pdf", KIND_ITEM, "sem conexão")
    assert explain(older).startswith("Item Project Root/Obras/a.pdf: a conexão")
