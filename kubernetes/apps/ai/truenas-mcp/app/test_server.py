import asyncio
import unittest
from unittest.mock import MagicMock, patch

import server


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'[{"id": "tank", "name": "tank", "secret": "hidden"}]'

    def test_fixed_get_endpoint_projection_and_pagination(self):
        with patch.dict("os.environ", TRUENAS_API_KEY="test-only", TRUENAS_CA_FILE="test.pem"), patch(
            "server.ssl.create_default_context"
        ) as tls, patch("server.http.client.HTTPSConnection", return_value=self.connection):
            result = server.query("datasets", 1, 0)
        tls.assert_called_once_with(cafile="test.pem")
        self.assertFalse(tls.return_value.check_hostname)
        self.connection.request.assert_called_once_with(
            "GET", "/api/v2.0/pool/dataset", headers={"Authorization": "Bearer test-only"}
        )
        self.assertEqual(result, {"items": [{"id": "tank", "name": "tank"}], "total": 1})
        self.connection.close.assert_called_once()

    def test_errors_omit_response_body(self):
        self.response.status = 401
        self.response.read.return_value = b"sensitive-response"
        with patch.dict("os.environ", TRUENAS_API_KEY="test-only", TRUENAS_CA_FILE="test.pem"), patch(
            "server.ssl.create_default_context"
        ), patch("server.http.client.HTTPSConnection", return_value=self.connection):
            with self.assertRaisesRegex(RuntimeError, "^TrueNAS returned HTTP 401$"):
                server.query("system", 1, 0)
        self.response.read.assert_not_called()
        self.connection.close.assert_called_once()

    def test_mcp_rejects_write_paths_and_invalid_pagination(self):
        async def check():
            tools = await server.mcp.list_tools()
            self.assertEqual([tool.name for tool in tools], ["truenas_query"])
            self.assertTrue(tools[0].annotations.read_only_hint)
            for args in ({"resource": "../system/reboot"}, {"resource": "pools", "limit": 101}, {"resource": "pools", "offset": -1}):
                with self.assertRaises(Exception), patch("server.query") as query:
                    await server.mcp.call_tool("truenas_query", args)
                query.assert_not_called()
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
