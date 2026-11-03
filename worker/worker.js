// ACE - Konto-Server (Cloudflare Worker + KV)
// Bindings: KV (KV-Namespace "KV"), Variable ADMIN_KEY (geheim), optional INSTALLER_URL
const enc = new TextEncoder();
const J = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json; charset=utf-8" } });
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const rnd = (n) => hex(crypto.getRandomValues(new Uint8Array(n)));
const NAME_RE = /^[A-Za-z0-9_.\-]{3,24}$/;
const DAY = 86400;

async function hash(pw, salt) {
  const key = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: enc.encode(salt), iterations: 100000 }, key, 256);
  return hex(bits);
}
function same(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
async function body(req) { try { return await req.json(); } catch { return {}; } }

async function newSession(env, name) {
  const t = rnd(32);
  await env.KV.put("tok:" + t, name, { expirationTtl: 30 * DAY });
  return t;
}
async function auth(req, env) {
  const h = req.headers.get("authorization") || "";
  const t = h.startsWith("Bearer ") ? h.slice(7) : "";
  if (!t) return null;
  const name = await env.KV.get("tok:" + t);
  if (!name) return null;
  const u = await env.KV.get("user:" + name, "json");
  if (!u || u.disabled) return null;
  return { name, role: u.role, token: t };
}
async function throttled(env, key) {
  const k = "fail:" + key;
  const n = parseInt((await env.KV.get(k)) || "0", 10);
  return n >= 8;
}
async function fail(env, key) {
  const k = "fail:" + key;
  const n = parseInt((await env.KV.get(k)) || "0", 10);
  await env.KV.put(k, String(n + 1), { expirationTtl: 900 });
}
function checkCreds(name, pw) {
  if (!NAME_RE.test(name || "")) return "Benutzername: 3-24 Zeichen (Buchstaben, Zahlen, _ . -).";
  if (!pw || pw.length < 6) return "Das Passwort braucht mindestens 6 Zeichen.";
  return null;
}
async function createUser(env, name, pw, role, label) {
  const salt = rnd(16);
  await env.KV.put("user:" + name, JSON.stringify({ salt, hash: await hash(pw, salt), role, label: label || "", created: Date.now() }));
}

function page(code, valid, installer) {
  const body = valid
    ? `<p>Dein Zugang ist bereit.</p>
       <a class="b" href="${installer}">1. ACE herunterladen</a>
       <p class="s">Installieren (Windows blendet evtl. eine Warnung ein: „Weitere Informationen“, dann „Trotzdem ausführen“).</p>
       <a class="b g" href="ace://invite/${code}">2. Zugang in der App aktivieren</a>
       <p class="s">Oder in der App den Einladungscode eingeben:</p><div class="c">${code}</div>`
    : `<p>Dieser Link ist ungültig oder wurde schon benutzt. Bitte frag nach einem neuen.</p>`;
  return `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>ACE</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,sans-serif;background:linear-gradient(135deg,#dfe1e8,#c9ccd6);color:#2a2848}
main{background:#ffffffcc;border-radius:28px;padding:36px;max-width:420px;box-shadow:0 20px 60px #0002;text-align:center}
h1{margin:0 0 8px}a.b{display:block;margin:14px 0 6px;padding:14px;border-radius:14px;background:#5f6575;color:#fff;text-decoration:none;font-weight:600}
a.g{background:#fff;color:#2a2848;border:1px solid #0002}.s{font-size:13px;opacity:.7}.c{font:700 20px ui-monospace,monospace;letter-spacing:1px;margin-top:6px;user-select:all}</style>
<main><h1>ACE</h1>${body}</main>`;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const m = req.method;
    try {
      if (m === "GET" && path === "/") return J({ ok: true, app: "kill-app" });

      if (m === "GET" && path.startsWith("/d/")) {
        const code = path.slice(3).replace(/[^A-Za-z0-9]/g, "");
        const inv = await env.KV.get("inv:" + code, "json");
        const inst = env.INSTALLER_URL || "https://github.com/Gaby18x/kill-app/releases/latest/download/ACE-Setup.exe";
        return new Response(page(code, !!inv && !inv.used, inst), { headers: { "content-type": "text/html; charset=utf-8" } });
      }

      if (m === "POST" && path === "/setup") {
        const b = await body(req);
        if (await env.KV.get("admin_exists")) return J({ error: "Es gibt schon einen Admin." }, 403);
        if (!env.ADMIN_KEY || !same(String(b.key || ""), env.ADMIN_KEY)) return J({ error: "Falscher Admin-Schlüssel." }, 403);
        const e = checkCreds(b.username, b.password);
        if (e) return J({ error: e }, 400);
        await createUser(env, b.username.toLowerCase(), b.password, "admin", "Admin");
        await env.KV.put("admin_exists", "1");
        return J({ ok: true });
      }

      if (m === "POST" && path === "/login") {
        const b = await body(req);
        const name = String(b.username || "").toLowerCase();
        if (await throttled(env, name)) return J({ error: "Zu viele Versuche. Bitte in 15 Minuten erneut probieren." }, 429);
        const u = await env.KV.get("user:" + name, "json");
        if (!u || u.disabled || !same(await hash(String(b.password || ""), u.salt), u.hash)) {
          await fail(env, name);
          return J({ error: "Benutzername oder Passwort stimmt nicht." }, 401);
        }
        return J({ token: await newSession(env, name), role: u.role, name });
      }

      if (m === "POST" && path === "/redeem") {
        const b = await body(req);
        const code = String(b.invite || "").replace(/[^A-Za-z0-9]/g, "");
        if (await throttled(env, "inv")) return J({ error: "Zu viele Versuche." }, 429);
        const inv = code && (await env.KV.get("inv:" + code, "json"));
        if (!inv || inv.used) { await fail(env, "inv"); return J({ error: "Der Einladungscode ist ungültig oder schon benutzt." }, 400); }
        const e = checkCreds(b.username, b.password);
        if (e) return J({ error: e }, 400);
        const name = b.username.toLowerCase();
        if (await env.KV.get("user:" + name)) return J({ error: "Diesen Benutzernamen gibt es schon." }, 409);
        await createUser(env, name, b.password, "user", inv.label);
        await env.KV.put("inv:" + code, JSON.stringify({ ...inv, used: true, by: name }), { expirationTtl: 30 * DAY });
        return J({ token: await newSession(env, name), role: "user", name });
      }

      const me = await auth(req, env);
      if (!me) return J({ error: "Nicht angemeldet." }, 401);

      if (m === "GET" && path === "/me") return J({ name: me.name, role: me.role });
      if (m === "POST" && path === "/logout") { await env.KV.delete("tok:" + me.token); return J({ ok: true }); }

      if (m === "GET" && path === "/catalog") {
        const c = await env.KV.get("catalog", "json");
        return J(c || { ver: 0, data: null });
      }

      if (me.role !== "admin") return J({ error: "Nur für den Admin." }, 403);

      if (m === "PUT" && path === "/catalog") {
        const b = await body(req);
        if (!b.data || typeof b.data !== "object") return J({ error: "Ungültige Daten." }, 400);
        const old = (await env.KV.get("catalog", "json")) || { ver: 0 };
        const c = { ver: old.ver + 1, data: b.data };
        await env.KV.put("catalog", JSON.stringify(c));
        return J({ ver: c.ver });
      }
      if (m === "POST" && path === "/invite") {
        const b = await body(req);
        const code = rnd(5).toUpperCase();
        await env.KV.put("inv:" + code, JSON.stringify({ label: String(b.label || "").slice(0, 40), used: false, created: Date.now() }), { expirationTtl: 14 * DAY });
        return J({ code, link: url.origin + "/d/" + code });
      }
      if (m === "GET" && path === "/users") {
        const l = await env.KV.list({ prefix: "user:" });
        const out = [];
        for (const k of l.keys) {
          const u = await env.KV.get(k.name, "json");
          if (u) out.push({ name: k.name.slice(5), role: u.role, label: u.label, created: u.created, disabled: !!u.disabled });
        }
        return J({ users: out });
      }
      if (m === "POST" && path === "/users/disable") {
        const b = await body(req);
        const name = String(b.name || "").toLowerCase();
        const u = await env.KV.get("user:" + name, "json");
        if (!u || u.role === "admin") return J({ error: "Nicht möglich." }, 400);
        u.disabled = !!b.disabled;
        await env.KV.put("user:" + name, JSON.stringify(u));
        return J({ ok: true });
      }
      if (m === "POST" && path === "/users/delete") {
        const b = await body(req);
        const name = String(b.name || "").toLowerCase();
        const u = await env.KV.get("user:" + name, "json");
        if (!u || u.role === "admin") return J({ error: "Nicht möglich." }, 400);
        await env.KV.delete("user:" + name);
        return J({ ok: true });
      }
      return J({ error: "Nicht gefunden." }, 404);
    } catch (e) {
      return J({ error: "Serverfehler." }, 500);
    }
  },
};
