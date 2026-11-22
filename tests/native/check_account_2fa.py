"""Verify account 2FA (TOTP + recovery codes + replay & session protection) on a disposable loopback node."""

import base64
import hashlib
import hmac
import json
import struct
import sys
import tempfile
import time
from pathlib import Path

from check_token_http import login, request, server, sign


def totp_code(secret_b32: str, unix_seconds: int, period: int = 30, digits: int = 6) -> str:
    padded = secret_b32 + ("=" * ((8 - len(secret_b32) % 8) % 8))
    secret = base64.b32decode(padded, casefold=True)
    counter = unix_seconds // period
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def signed_login(connection, oracle, keys, account, *, client_id="client-2fa", status=200,
                 two_factor_header=None, extra_body=None):
    _, challenge = request(connection, "GET", f"/api/v1/sync/challenge?user_id={account}")
    payload = {"user_id_hash": account, "client_id": client_id, "public_key": keys["PublicKey"]}
    if extra_body:
        payload.update(extra_body)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    msg = f"mesh-sync-v1\nPOST\n/api/v1/sync/login\n{hashlib.sha256(raw).hexdigest()}\n{challenge['nonce']}\n".encode()
    sig = sign(oracle, keys, msg)
    headers = {"X-Mesh-User": account, "X-Mesh-Signature": sig, "Content-Type": "application/json"}
    if two_factor_header:
        headers["X-Mesh-2FA"] = two_factor_header
    return request(connection, "POST", "/api/v1/sync/login", status=status, body=raw, headers=headers)


def signed_delete(connection, oracle, keys, account, *, status=200, two_factor_header=None):
    _, challenge = request(connection, "GET", f"/api/v1/sync/challenge?user_id={account}")
    raw = json.dumps({"user_id_hash": account}, separators=(",", ":")).encode()
    msg = f"mesh-sync-v1\nPOST\n/api/v1/account/delete\n{hashlib.sha256(raw).hexdigest()}\n{challenge['nonce']}\n".encode()
    sig = sign(oracle, keys, msg)
    headers = {"X-Mesh-User": account, "X-Mesh-Signature": sig, "Content-Type": "application/json"}
    if two_factor_header:
        headers["X-Mesh-2FA"] = two_factor_header
    return request(connection, "POST", "/api/v1/account/delete", status=status, body=raw, headers=headers)


def run_checks(binary: str, oracle: str):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mesh-2fa.db"
        with server(binary, db) as (conn, store):
            account, token1, keys = login(conn, oracle)
            _, second = signed_login(conn, oracle, keys, account, client_id="other-device")
            token2 = second["auth_token"]

            # 1. Initially disabled.
            resp, status = request(conn, "GET", "/api/v1/account/2fa", token=token1)
            assert resp.getheader("Cache-Control") == "private, no-store"
            assert status == {"enabled": False, "pending": False, "methods": [], "recovery_codes_remaining": 0}, status

            # 2. Setup TOTP -> pending secret + otpauth URI.
            _, setup = request(conn, "POST", "/api/v1/account/2fa/totp/setup", body={}, token=token1)
            secret = setup["secret"]
            assert len(secret) == 32, setup
            assert setup["otpauth_uri"].startswith(f"otpauth://totp/Mesh:{account[:12]}?secret={secret}&issuer=Mesh"), setup
            assert setup["digits"] == 6 and setup["period"] == 30

            # Secret stored encrypted (never plaintext Base32 in SQLite).
            row = store.execute(
                "SELECT enabled, pending_secret_enc FROM server_account_2fa WHERE user_id_hash=?",
                (account,),
            ).fetchone()
            assert row is not None and row[0] == 0 and len(row[1]) == 120 and secret not in row[1]

            _, pending = request(conn, "GET", "/api/v1/account/2fa", token=token1)
            assert pending["enabled"] is False and pending["pending"] is True

            # 3. Confirm rejects wrong code, accepts valid code + revokes other sessions.
            request(conn, "POST", "/api/v1/account/2fa/totp/confirm", status=401,
                    body={"code": "000000"}, token=token1)
            now = int(time.time())
            code_now = totp_code(secret, now)
            _, confirmed = request(conn, "POST", "/api/v1/account/2fa/totp/confirm",
                                   body={"code": code_now, "revoke_other_sessions": True}, token=token1)
            assert confirmed["enabled"] is True
            recovery_codes = confirmed["recovery_codes"]
            assert len(recovery_codes) == 10 and all(len(c) == 19 and c.count("-") == 3 for c in recovery_codes)

            # Setup again returns 409 already enabled.
            request(conn, "POST", "/api/v1/account/2fa/totp/setup", status=409, body={}, token=token1)

            # Other session was revoked; current session still valid.
            request(conn, "GET", "/api/v1/account/2fa", status=401, token=token2)
            _, active = request(conn, "GET", "/api/v1/account/2fa", token=token1)
            assert active == {
                "enabled": True,
                "pending": False,
                "methods": ["totp", "recovery_code"],
                "recovery_codes_remaining": 10,
            }, active

            # 4. Login with stolen private key alone is blocked (401 2fa_required).
            _, missing = signed_login(conn, oracle, keys, account, status=401)
            assert missing == {"error": "2fa_required", "methods": ["totp", "recovery_code"]}, missing

            # Replaying the just-used TOTP code from confirm is rejected!
            _, replayed = signed_login(conn, oracle, keys, account, status=401, two_factor_header=code_now)
            assert replayed == {"error": "invalid 2fa code"}, replayed

            # Wrong TOTP code is rejected.
            _, wrong = signed_login(conn, oracle, keys, account, status=401, two_factor_header="999999")
            assert wrong == {"error": "invalid 2fa code"}, wrong

            # Valid recovery code succeeds via X-Mesh-2FA header and is single-use.
            _, ok_login = signed_login(conn, oracle, keys, account, status=200,
                                       two_factor_header=recovery_codes[0].lower().replace("-", " "))
            assert ok_login["status"] == "ok" and ok_login["auth_token"]
            token3 = ok_login["auth_token"]

            # Replaying the consumed recovery code fails.
            signed_login(conn, oracle, keys, account, status=401, two_factor_header=recovery_codes[0])

            _, after_use = request(conn, "GET", "/api/v1/account/2fa", token=token3)
            assert after_use["recovery_codes_remaining"] == 9

            # 5. Signed account delete without 2FA is blocked even with valid ML-DSA-44 signature.
            _, del_blocked = signed_delete(conn, oracle, keys, account, status=401)
            assert del_blocked == {"error": "2fa_required", "methods": ["totp", "recovery_code"]}, del_blocked

            # 6. Regenerate recovery codes requires valid 2FA code, invalidates old codes.
            request(conn, "POST", "/api/v1/account/2fa/recovery/regenerate", status=401,
                    body={}, token=token3)
            _, regen = request(conn, "POST", "/api/v1/account/2fa/recovery/regenerate",
                               body={"code": recovery_codes[1]}, token=token3)
            new_codes = regen["recovery_codes"]
            assert len(new_codes) == 10 and new_codes[0] != recovery_codes[2]
            # Old unused code no longer works.
            signed_login(conn, oracle, keys, account, status=401, two_factor_header=recovery_codes[2])

            # 7. Disable requires valid 2FA code; once disabled, normal login works again.
            request(conn, "POST", "/api/v1/account/2fa/disable", status=401, body={}, token=token3)
            _, disabled = request(conn, "POST", "/api/v1/account/2fa/disable",
                                  body={"code": new_codes[0]}, token=token3)
            assert disabled == {"enabled": False}
            _, back_to_normal = signed_login(conn, oracle, keys, account, status=200)
            assert back_to_normal["status"] == "ok"

            # 8. Brute-force rate limit triggers 429 after 10 attempts per user.
            acct2, tok2, keys2 = login(conn, oracle)
            _, setup2 = request(conn, "POST", "/api/v1/account/2fa/totp/setup", body={}, token=tok2)
            code2 = totp_code(setup2["secret"], int(time.time()))
            request(conn, "POST", "/api/v1/account/2fa/totp/confirm", body={"code": code2}, token=tok2)
            for _ in range(9):
                signed_login(conn, oracle, keys2, acct2, status=401, two_factor_header="000000")
            _, limited = signed_login(conn, oracle, keys2, acct2, status=429, two_factor_header="000000")
            assert limited == {"error": "2fa rate limit exceeded"}, limited

    print("Mesh native account 2FA passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} <server_native> <signing_oracle>")
    run_checks(sys.argv[1], sys.argv[2])
