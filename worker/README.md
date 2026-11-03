# Konto-Server einrichten (Cloudflare, kostenlos)

1. Auf https://dash.cloudflare.com registrieren (kostenlos).
2. **Workers & Pages → KV → Create** → Namespace `killapp` anlegen.
3. **Workers & Pages → Create → Worker** → Name z. B. `killapp`, deployen, dann **Edit code**: den kompletten Inhalt von `worker.js` einfügen, **Deploy**.
4. Im Worker: **Settings → Bindings → Add → KV namespace**: Variablenname `KV`, Namespace `killapp`.
5. **Settings → Variables and Secrets → Add**: Typ *Secret*, Name `ADMIN_KEY`, Wert = ein langes geheimes Passwort (nur du kennst es).
6. Die Adresse des Workers (`https://killapp.<dein-name>.workers.dev`) in `core.py` bei `ACCOUNT_URL` eintragen.
7. Admin-Konto einmalig anlegen (Windows PowerShell):

```
Invoke-RestMethod -Method Post -Uri https://killapp.DEIN-NAME.workers.dev/setup -ContentType application/json -Body '{"key":"DEIN_ADMIN_KEY","username":"gaby","password":"DEIN_PASSWORT"}'
```

Danach in der App mit diesem Benutzernamen anmelden. Unter Einstellungen → Konto erstellst du Einladungslinks.
