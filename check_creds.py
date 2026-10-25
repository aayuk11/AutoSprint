"""Pre-flight check for the Zoho service account.

Verifies ONE credential set can do both jobs - Zoho Sheet storage and Cliq
posting (message + file upload) - and that the account is actually a member of
every channel the projects post to.

Run from the repo root:      python check_creds.py
Optional real post test:     python check_creds.py --post <channel-unique-name>

Prints PASS/FAIL, scopes and channel names only - never a credential value.
Without --post nothing is sent: the upload check posts an empty body, which
fails on payload once the scope is right.
"""
import hashlib
import sys

import requests

from modules import config, store

SHEET_SCOPES = ["ZohoSheet.dataAPI.READ", "ZohoSheet.dataAPI.UPDATE"]
CLIQ_SCOPES = ["ZohoCliq.Channels.READ", "ZohoCliq.Chats.ALL",
               "ZohoCliq.Messages.CREATE", "ZohoCliq.Webhooks.CREATE"]

passed = True


def ok(label, msg=""):
    print(f"  [PASS] {label}" + (f" - {msg}" if msg else ""))


def bad(label, msg=""):
    global passed
    passed = False
    print(f"  [FAIL] {label}" + (f" - {msg}" if msg else ""))


def warn(label, msg=""):
    print(f"  [WARN] {label}" + (f" - {msg}" if msg else ""))


def fp(v):
    return hashlib.sha256((v or "").encode()).hexdigest()[:8]


def main():
    post_to = None
    if "--post" in sys.argv:
        post_to = sys.argv[sys.argv.index("--post") + 1]

    zoho, cliq = config.section("zoho"), config.section("cliq")
    if not zoho or not cliq:
        bad("config", f"zoho missing {config.missing('zoho')}, cliq missing {config.missing('cliq')}")
        return 1

    print("\n1. One credential for both jobs")
    same = all(fp(zoho.get(k)) == fp(cliq.get(k))
               for k in ("client_id", "client_secret", "refresh_token"))
    if same:
        ok("CLIQ_* and ZOHO_* are the same client", f"fingerprint {fp(zoho['client_id'])}")
    else:
        warn("CLIQ_* and ZOHO_* are DIFFERENT clients",
             "intended for the service account? both must be sprint.sheet@")

    print("\n2. Token and scopes")
    dc = cliq.get("location", "in")
    r = requests.post(f"https://accounts.zoho.{dc}/oauth/v2/token", data={
        "refresh_token": cliq["refresh_token"], "client_id": cliq["client_id"],
        "client_secret": cliq["client_secret"], "grant_type": "refresh_token"}, timeout=20)
    body = r.json()
    if "access_token" not in body:
        bad("refresh token rejected", str(body))
        return 1
    tok, scope = body["access_token"], body.get("scope", "")
    ok("refresh token valid", f"data centre .{dc}")
    print(f"  granted: {scope}")
    for s in SHEET_SCOPES + CLIQ_SCOPES:
        (ok if s in scope else bad)(s, "" if s in scope else "MISSING")

    h = {"Authorization": f"Zoho-oauthtoken {tok}"}
    api = f"https://cliq.zoho.{dc}"

    print("\n3. Which account is this?")
    me = requests.get(f"{api}/api/v2/users/me", headers=h, timeout=20).json()
    d = me.get("data") or me
    who = d.get("email_id") or d.get("name")
    if who:
        print(f"  posting as: {who}")
    else:
        warn("identity not readable", "needs ZohoCliq.Users.READ - not required, skipping")

    print("\n4. Zoho Sheet storage")
    try:
        b = store.get_backend()
        cfgs = b.load_all("project_configs") or {}
        chans = b.load_all("zoho_channels") or {}
        ok("sheet readable", f"{len(cfgs)} project_configs, {len(chans)} channels mapped")
    except Exception as exc:
        bad("sheet read failed", str(exc)[:200])
        print("\n  -> share the sheet with your service account (EDIT rights)")
        return 1

    if "--writetest" in sys.argv:
        # Proves EDIT rights, not just view. The store is append-only, so this
        # appends a row under a throwaway project then tombstones it: the app
        # never sees it again (load_all skips tombstones). Two inert rows are
        # left in the sheet.
        probe = "__writetest__"
        try:
            b.save_one("zoho_channels", probe, "writetest")
            back = (b.load_all("zoho_channels") or {}).get(probe)
            b.delete_one("zoho_channels", probe)
            gone = probe not in (b.load_all("zoho_channels") or {})
            if back == "writetest" and gone:
                ok("sheet writable", "wrote, read back and tombstoned a test row")
            else:
                bad("sheet write", f"wrote but read back {back!r}, cleaned={gone}")
        except Exception as exc:
            bad("sheet write failed", str(exc)[:200])
            print("  -> the share is probably VIEW only; it must be EDIT")
    else:
        warn("sheet write not tested", "re-run with --writetest to prove EDIT rights")

    print("\n5. Channel membership (the account can only post where it is a member)")
    # /channels returns 50 per page with has_more/next_token - a single call
    # silently truncates and reports channels as "not a member" that are.
    joined, first, token, pages = {}, None, None, 0
    while True:
        params = {"limit": 99}
        if token:
            params["next_token"] = token
        resp = requests.get(f"{api}/api/v2/channels", headers=h,
                            params=params, timeout=30).json()
        batch = resp.get("channels") or []
        first = first or (batch[0] if batch else None)
        for c in batch:
            joined[c.get("unique_name")] = c.get("name")
        pages += 1
        token = resp.get("next_token")
        if not batch or not resp.get("has_more") or pages > 20:
            break
    ok("channel list readable", f"member of {len(joined)} channels ({pages} page(s))")
    targets = {p: c for p, c in chans.items() if c}
    if not targets:
        warn("no project -> channel mappings found in the sheet")
    for project, raw in sorted(targets.items()):
        ch = (raw or "").strip().lstrip("#").strip()
        if "/" in ch or " " in ch:
            bad(f"{project}", f"malformed mapping {raw!r} - must be the channel "
                              f"UNIQUE name (no #, no spaces, no URL)")
        elif ch in joined:
            ok(f"{project}", f"#{ch}")
        else:
            bad(f"{project}", f"#{ch} - account is NOT a member, posting will fail")

    print("\n6. File upload scope")
    if joined:
        chat_id = first["chat_id"]
        u = requests.post(f"{api}/api/v2/chats/{chat_id}/files", headers=h, files={}, timeout=30)
        if u.status_code == 401 and "scope" in u.text:
            bad("upload scope", "401 oauthtoken_scope_invalid")
        else:
            ok("upload scope", f"accepted (HTTP {u.status_code} = payload check, nothing posted)")

    if post_to:
        print(f"\n7. REAL post to #{post_to}")
        from modules import cliq_client
        warns = cliq_client.post_report(
            post_to, [("scope-test.txt", b"sprint sheet service account test", "text/plain")],
            message="Credential test - please ignore.")
        if warns:
            bad("real upload", " | ".join(warns)[:300])
        else:
            ok("real message + file upload")

    print("\n" + ("ALL CHECKS PASSED" if passed else "SOME CHECKS FAILED"))
    return 0 if passed else 1


sys.exit(main())
