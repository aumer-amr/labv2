"""Check the opt-in legacy certificate loader without HA or device I/O."""

import ast
import json
import re
import ssl
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

source = ast.parse(Path(sys.argv[1]).read_text())
loader = next(
    (node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "_legacy_credentials"),
    None,
)
assert loader is not None, "Legacy application-certificate loader is missing"

with tempfile.TemporaryDirectory() as directory:
    bundle = Path(directory) / "legacy.pem"
    validate = Mock()
    mint = Mock(return_value=("minted certificate", "minted key"))
    scope = {
        "Path": lambda _: bundle,
        "re": re,
        "ssl": ssl,
        "InvalidCA": ValueError,
        "client_context": validate,
        "_mint_preferred": mint,
    }
    exec(compile(ast.Module(body=[loader], type_ignores=[]), "loader", "exec"), scope)
    load = scope["_legacy_credentials"]
    assert load("CA", "KEY", None) == mint.return_value
    mint.assert_called_once_with("CA", "KEY")
    assert load("CA", "KEY", ("existing", "key")) == ("existing", "key")

    cert = "-----BEGIN CERTIFICATE-----\ntest certificate\n-----END CERTIFICATE-----"
    key = "-----BEGIN PRIVATE KEY-----\ntest key\n-----END PRIVATE KEY-----"
    bundle.write_text(key + "\n" + cert)
    assert load("", "", ("unrelated", "key")) == (cert + "\n", key + "\n")
    validate.assert_called_once_with(cert + "\n", key + "\n")

    for invalid in ("invalid", cert, key, key + "\n" + key + "\n" + cert, "x" * 65537):
        bundle.write_text(invalid)
        try:
            load("", "", None)
        except ValueError as error:
            assert str(error) == "Invalid legacy application certificate bundle"
        else:
            raise AssertionError("Invalid bundle was accepted")

    bundle.write_text(key + "\n" + cert)
    validate.side_effect = ssl.SSLError("private material must not escape")
    try:
        load("", "", None)
    except ValueError as error:
        assert str(error) == "Invalid legacy application certificate bundle"
    else:
        raise AssertionError("Certificate/key validation was skipped")

print("PASS: legacy bundle selection, default behavior, validation and error redaction")

token_source = ast.parse(Path(sys.argv[2]).read_text())
request = next(node for node in token_source.body if isinstance(node, ast.FunctionDef) and node.name == "request_token")
connection = Mock()
connection.getresponse.return_value.status = 200
request_scope = {"HTTPSConnection": Mock(return_value=connection), "_PLACEHOLDER_TOKEN": "placeholder", "CALLBACK_PORT": 8889, "ssl": ssl, "json": json}
exec(compile(ast.Module(body=[request], type_ignores=[]), "request", "exec"), request_scope)
assert request_scope["request_token"]("10.40.0.224", 8888, "10.42.2.20", object()) == 200
connection.putheader.assert_any_call("Host", "10.42.2.20:8889")
connection.putheader.assert_any_call("Content-Length", "0")
connection.endheaders.assert_called_once_with(b"")
connection.close.assert_called_once()
print("PASS: legacy token request has an empty body and explicit callback address")
