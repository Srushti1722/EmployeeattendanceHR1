import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pymongo.errors import PyMongoError

from app import main


class HealthEndpointTests(unittest.TestCase):
    def test_returns_success_when_mongodb_ping_succeeds(self):
        mongo_client = Mock()
        with patch.object(main, "client", mongo_client):
            self.assertEqual(main.health(), {"status": "ok"})

        mongo_client.admin.command.assert_called_once_with("ping")

    def test_returns_service_unavailable_when_mongodb_ping_fails(self):
        mongo_client = Mock()
        mongo_client.admin.command.side_effect = PyMongoError(
            "simulated MongoDB failure"
        )
        with patch.object(main, "client", mongo_client):
            with self.assertRaises(HTTPException) as raised:
                main.health()

        mongo_client.admin.command.assert_called_once_with("ping")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "MongoDB unavailable")


if __name__ == "__main__":
    unittest.main()
