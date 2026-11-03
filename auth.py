"""ACE - Konto-Client (kein GUI). Spricht mit dem Cloudflare-Worker."""
import base64
import json
import os
import urllib.error
import urllib.request

import core


class ApiError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class Offline(ApiError):
    pass


def call(method, path, data=None, token=None, timeout=15):
    url = core.ACCOUNT_URL.rstrip("/") + path
    h = {"content-type": "application/json", "user-agent": "ACE"}
    if token:
        h["authorization"] = "Bearer " + token
    req = urllib.request.Request(url, method=method, headers=h,
                                 data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", "Fehler")
        except Exception:
            msg = "Fehler"
        raise ApiError(msg, e.code)
    except Exception:
        raise Offline("Keine Verbindung zum Konto-Server.")


# ---- Katalog (Server-Liste inkl. Bilder) <-> lokale Dateien
def _b64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except OSError:
        return None


def pack_catalog(st, avatar_path, builtin_ids=()):
    files, avatars = {}, {}
    for s in st.get("servers", []):
        d = os.path.join(core.DATA_DIR, "servers", s["id"])
        if os.path.isdir(d):
            files[s["id"]] = {n: _b64(os.path.join(d, n)) for n in os.listdir(d) if n.endswith(".png")}
        b = _b64(avatar_path(s["id"]))
        if b:
            avatars[s["id"]] = b
    for sid in builtin_ids:
        b = _b64(avatar_path(sid))
        if b:
            avatars[sid] = b
    return {"servers": st.get("servers", []), "names": st.get("names", {}), "hidden": st.get("hidden", []),
            "themes": st.get("themes", {}), "files": files, "avatars": avatars}


def unpack_catalog(data, st, avatar_path, builtin_ids=()):
    import shutil
    old = {s["id"] for s in st.get("servers", [])}
    new = {s["id"] for s in data.get("servers", [])}
    for sid in old - new:
        shutil.rmtree(os.path.join(core.DATA_DIR, "servers", sid), ignore_errors=True)
        try:
            os.remove(avatar_path(sid))
        except OSError:
            pass
    for sid, fl in data.get("files", {}).items():
        d = os.path.join(core.DATA_DIR, "servers", sid)
        os.makedirs(d, exist_ok=True)
        for n, b in fl.items():
            if b and os.path.basename(n) == n:
                with open(os.path.join(d, n), "wb") as f:
                    f.write(base64.b64decode(b))
    allav = set(data.get("avatars", {})) | new | set(builtin_ids)
    for sid in allav:
        b = data.get("avatars", {}).get(sid)
        p = avatar_path(sid)
        if b:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(base64.b64decode(b))
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    st["servers"] = data.get("servers", [])
    st["names"] = data.get("names", {})
    st["hidden"] = data.get("hidden", [])
    st["themes"] = data.get("themes", {})
