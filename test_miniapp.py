"""Testes da validação de initData do Mini App (miniapp.py)."""

import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import parse_qsl, urlencode

import miniapp

BOT_TOKEN = "123456:ABC-fake-token-for-tests"


def _build_init_data(fields, bot_token=BOT_TOKEN):
    """Monta uma string initData genuína (mesmo algoritmo do Telegram),
    pra testar a validação com um dado que deveria passar."""
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": computed_hash})


class ValidateInitDataTest(unittest.TestCase):
    def test_accepts_genuine_init_data_and_returns_user(self):
        user = {"id": 12345, "first_name": "San"}
        init_data = _build_init_data({
            "auth_date": str(int(time.time())),
            "query_id": "AAabc123",
            "user": json.dumps(user),
        })

        result = miniapp.validate_init_data(init_data, BOT_TOKEN)

        self.assertEqual(result, user)

    def test_rejects_tampered_field(self):
        init_data = _build_init_data({
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 1}),
        })
        # Troca o conteúdo depois de gerado, sem recalcular o hash — simula
        # alguém adulterando o campo "user" pra se passar por outro usuário.
        fields = dict(parse_qsl(init_data))
        fields["user"] = json.dumps({"id": 999})
        tampered = urlencode(fields)

        self.assertIsNone(miniapp.validate_init_data(tampered, BOT_TOKEN))

    def test_rejects_wrong_bot_token(self):
        init_data = _build_init_data({
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 1}),
        })

        self.assertIsNone(miniapp.validate_init_data(init_data, "outro-token-qualquer"))

    def test_rejects_expired_auth_date(self):
        old_timestamp = int(time.time()) - miniapp.INIT_DATA_MAX_AGE_SECONDS - 3600
        init_data = _build_init_data({
            "auth_date": str(old_timestamp),
            "user": json.dumps({"id": 1}),
        })

        self.assertIsNone(miniapp.validate_init_data(init_data, BOT_TOKEN))

    def test_rejects_missing_hash(self):
        self.assertIsNone(miniapp.validate_init_data("auth_date=123&user=%7B%7D", BOT_TOKEN))

    def test_rejects_empty_or_missing_input(self):
        self.assertIsNone(miniapp.validate_init_data("", BOT_TOKEN))
        self.assertIsNone(miniapp.validate_init_data("auth_date=123&hash=abc", ""))


if __name__ == "__main__":
    unittest.main()
