import pytest

from bimcloud_backup import i18n
from bimcloud_backup.auth import Tokens

SERVER = "https://example.bimcloud.com"
OAUTH = f"{SERVER}/management/client/oauth2"
API = f"{SERVER}/management/client"


def token_response(access="access-1", refresh="refresh-1"):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "access_token_exp": 1700000000,
        "token_type": "Bearer",
        "user_id": "user-1",
    }


class MemoryTokenStore:
    def __init__(self, token=None):
        self.tokens = {SERVER: token} if token else {}

    def load(self, server_url):
        return self.tokens.get(server_url)

    def save(self, server_url, refresh_token):
        self.tokens[server_url] = refresh_token

    def delete(self, server_url):
        self.tokens.pop(server_url, None)


@pytest.fixture
def tokens():
    return Tokens.from_response(token_response())


# Most tests check the Portuguese texts, including windows built once per module, before any
# function fixture runs. Tests of English set it themselves and `portuguese` puts it back.
i18n.set_language(i18n.PORTUGUESE)


@pytest.fixture(autouse=True)
def portuguese():
    i18n.set_language(i18n.PORTUGUESE)
    yield
    i18n.set_language(i18n.PORTUGUESE)
