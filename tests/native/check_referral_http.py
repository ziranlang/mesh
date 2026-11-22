"""Exercise native referral routes on the canonical issuer with disposable SQLite data."""

import sys
import tempfile
from pathlib import Path

from check_token_http import SEED, login, request, server


AUTHORITY = "https://api.waozi.xyz"
ME, CLAIM, ACCEPTED, ROTATE = ("/api/v1/referrals/" + name for name in ("me", "claim", "accepted", "code/rotate"))
WELCOME = 20_000_000
ME_FIELDS = ["status", "authority", "account_id", "code", "invite_url", "referrer_id", "accepted_at",
             "welcome_receipt_id", "accepted_count", "earned_units", "reversed_units", "pending_offset_units"]
CLAIM_FIELDS = ["status", "referrer_id", "accepted_at", "welcome_units", "welcome_receipt_id"]


def call(connection, method, path, status=200, **kwargs):
    reply, payload = request(connection, method, path, status, **kwargs)
    assert reply.getheader("Cache-Control") == "private, no-store", (method, path, reply.getheaders())
    return payload


def failure(connection, method, path, status, error, **kwargs):
    assert call(connection, method, path, status, **kwargs) == {"error": error}


def check_refusals(binary, oracle, root):
    with server(binary, root / "wrong.sqlite", MESH_BASE_URL="https://elsewhere.invalid",
                MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED) as (connection, _):
        call(connection, "GET", ME, 401)
        _, bearer, _ = login(connection, oracle)
        assert call(connection, "GET", ME, 409, token=bearer) == {"error": "wrong_node", "authority": AUTHORITY}
    with server(binary, root / "unsigned.sqlite", MESH_BASE_URL=AUTHORITY) as (connection, _):
        _, bearer, _ = login(connection, oracle)
        failure(connection, "GET", ME, 503, "unavailable", token=bearer)


def claim(connection, bearer, code, request_id, status=200, **extra):
    return call(connection, "POST", CLAIM, status, token=bearer, body={"code": code, "request_id": request_id, **extra})


def check_codes_and_claims(connection, store, referrer, referrer_bearer, friend, friend_bearer, third_bearer):
    me = call(connection, "GET", ME, token=referrer_bearer)
    assert list(me) == ME_FIELDS, me
    code = me["code"]
    assert len(code) == 32 and code == code.lower() and me["invite_url"] == "https://dew.waozi.xyz/invite/" + code
    assert (me["status"], me["authority"], me["account_id"]) == ("ok", AUTHORITY, referrer)
    assert [me[name] for name in ME_FIELDS[5:]] == ["", "", "", 0, 0, 0, 0]
    assert call(connection, "GET", ME, token=referrer_bearer)["code"] == code

    failure(connection, "POST", CLAIM, 400, "invalid_request", token=friend_bearer, body={"code": code, "request_id": "short"})
    failure(connection, "POST", CLAIM, 400, "invalid_request", token=friend_bearer,
            body={"code": code, "request_id": "claim-0001", "extra": "field"})
    failure(connection, "POST", CLAIM, 400, "invalid_request", token=friend_bearer, body={"code": code[:31], "request_id": "claim-0001"})
    failure(connection, "POST", CLAIM, 404, "invalid_code", token=friend_bearer, body={"code": "0" * 32, "request_id": "claim-0001"})
    failure(connection, "POST", CLAIM, 409, "self_referral", token=referrer_bearer, body={"code": code, "request_id": "self-0001"})

    bound = claim(connection, friend_bearer, f"  {code.upper()} ", "claim-0001", source_app_id="inbe")
    assert list(bound) == CLAIM_FIELDS, bound
    assert (bound["status"], bound["referrer_id"], bound["welcome_units"]) == ("bound", referrer, WELCOME)
    assert len(bound["welcome_receipt_id"]) == 32 and bound["accepted_at"].endswith("Z")
    ledger = store.execute("SELECT account_id,app_id,event_type,amount_delta,source_type,source_ref FROM token_ledger "
                           "WHERE receipt_id=?", (bound["welcome_receipt_id"],)).fetchall()
    assert ledger == [(friend, "", "credit", WELCOME, "referral_welcome", friend)], ledger
    assert store.execute("SELECT referrer_id,code,request_id,source_app_id,welcome_seq>0 FROM referral_claims "
                         "WHERE account_id=?", (friend,)).fetchall() == [(referrer, code, "claim-0001", "inbe", 1)]

    again = claim(connection, friend_bearer, code, "claim-0002")
    assert again == dict(bound, status="already_bound"), again

    rotated = call(connection, "POST", ROTATE, token=referrer_bearer, body={"request_id": "rotate-0001"})
    assert list(rotated) == ["code", "invite_url"] and rotated["code"] != code
    assert call(connection, "POST", ROTATE, token=referrer_bearer, body={"request_id": "rotate-0001"}) == rotated
    failure(connection, "POST", ROTATE, 400, "invalid_request", token=referrer_bearer, body={"request_id": "tiny"})
    assert call(connection, "GET", ME, token=referrer_bearer)["code"] == rotated["code"]

    failure(connection, "POST", CLAIM, 409, "already_bound", token=friend_bearer, body={"code": rotated["code"], "request_id": "claim-0003"})
    failure(connection, "POST", CLAIM, 409, "request_conflict", token=friend_bearer, body={"code": rotated["code"], "request_id": "claim-0001"})
    failure(connection, "POST", CLAIM, 409, "revoked_code", token=third_bearer, body={"code": code, "request_id": "claim-0004"})
    third = claim(connection, third_bearer, rotated["code"], "claim-0004")
    assert third["status"] == "bound" and third["referrer_id"] == referrer
    assert store.execute("SELECT COUNT(*) FROM token_ledger WHERE source_type='referral_welcome'").fetchone() == (2,)
    return bound, third


def check_listing(connection, referrer_bearer, friend, friend_bearer, bound, third):
    """`third` is the second friend's account ID; both accepted in this order."""
    first = call(connection, "GET", ACCEPTED, token=referrer_bearer)
    assert [list(item) for item in first["accepted"]] == [["account_id", "display_name", "alias", "accepted_at", "cursor"]] * 2
    assert [item["account_id"] for item in first["accepted"]] == [friend, third]
    assert first["accepted"][0] == {"account_id": friend, "display_name": friend, "alias": "",
                                    "accepted_at": bound["accepted_at"], "cursor": first["accepted"][0]["cursor"]}
    assert first["next_cursor"] == ""
    page = call(connection, "GET", ACCEPTED + "?limit=1", token=referrer_bearer)
    assert page["accepted"] == first["accepted"][:1] and page["next_cursor"] == str(first["accepted"][0]["cursor"])
    rest = call(connection, "GET", f"{ACCEPTED}?cursor={page['next_cursor']}&limit=1", token=referrer_bearer)
    assert rest == {"accepted": first["accepted"][1:], "next_cursor": ""}
    for query in ("?limit=0", "?limit=101", "?cursor=-1", "?limit=many"):
        failure(connection, "GET", ACCEPTED + query, 400, "invalid_request", token=referrer_bearer)
    assert call(connection, "GET", ACCEPTED, token=friend_bearer) == {"accepted": [], "next_cursor": ""}

    mine = call(connection, "GET", ME, token=referrer_bearer)
    assert mine["accepted_count"] == 2 and mine["referrer_id"] == ""
    theirs = call(connection, "GET", ME, token=friend_bearer)
    assert [theirs[name] for name in ("referrer_id", "accepted_at", "welcome_receipt_id")] == [
        bound["referrer_id"], bound["accepted_at"], bound["welcome_receipt_id"]]


def check_methods(connection, bearer):
    for path, allowed in ((ME, "GET, HEAD"), (ACCEPTED, "GET, HEAD"), (CLAIM, "POST"), (ROTATE, "POST")):
        wrong = "POST" if allowed != "POST" else "GET"
        connection.request(wrong, path, headers={"Authorization": "Bearer " + bearer})
        reply = connection.getresponse()
        reply.read()
        assert (reply.status, reply.getheader("Allow")) == (405, allowed), (path, reply.status)
    connection.request("HEAD", ME, headers={"Authorization": "Bearer " + bearer})
    reply = connection.getresponse()
    assert (reply.status, reply.read()) == (200, b"")


def main(binary, oracle):
    binary, oracle = (str(Path(name).resolve()) for name in (binary, oracle))
    with tempfile.TemporaryDirectory(prefix="referral-", dir=Path(binary).parent) as temporary:
        root = Path(temporary)
        check_refusals(binary, oracle, root)
        with server(binary, root / "referrals.sqlite", MESH_BASE_URL=AUTHORITY,
                    MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED) as (connection, store):
            referrer, referrer_bearer, _ = login(connection, oracle)
            friend, friend_bearer, _ = login(connection, oracle)
            third, third_bearer, _ = login(connection, oracle)
            bound, _ = check_codes_and_claims(connection, store, referrer, referrer_bearer, friend, friend_bearer, third_bearer)
            check_listing(connection, referrer_bearer, friend, friend_bearer, bound, third)
            check_methods(connection, referrer_bearer)
    print("Native referral HTTP: canonical authority, codes, rotation, claims, welcome credit and listing passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
