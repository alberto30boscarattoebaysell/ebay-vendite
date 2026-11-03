#!/usr/bin/env python3
"""
Collega il tuo account eBay, direttamente da GitHub Actions (niente terminale).

- Senza URL: scrive nel riepilogo dell'esecuzione il link da aprire per
  autorizzare l'app su eBay.
- Con l'URL della pagina finale di eBay: scambia il codice con un refresh
  token e lo salva come secret EBAY_REFRESH_TOKEN del repository.
  Il token non viene mai stampato nei log.
"""

import base64
import os
import subprocess
import sys
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import requests

CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
RUNAME = os.environ.get("EBAY_RUNAME", "").strip()
PASTED = os.environ.get("AUTH_URL", "").strip()
REPO = os.environ["GITHUB_REPOSITORY"]
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/stdout")
SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"


def summary(text: str) -> None:
    with open(SUMMARY, "a") as f:
        f.write(text + "\n")


missing = [n for n, v in {
    "EBAY_CLIENT_ID (variabile)": CLIENT_ID,
    "EBAY_RUNAME (variabile)": RUNAME,
    "EBAY_CLIENT_SECRET (secret)": CLIENT_SECRET,
}.items() if not v]
if missing:
    summary("## ❌ Impostazioni mancanti\n\n" + "\n".join(f"- {m}" for m in missing))
    sys.exit(1)

# ---- Passo A: mostra il link di autorizzazione
if not PASTED:
    url = "https://auth.ebay.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": RUNAME,
        "scope": SCOPE,
    })
    summary(
        "## 1️⃣ Apri questo link e accetta\n\n"
        f"[👉 Autorizza l'app su eBay]({url})\n\n"
        "Dopo aver accettato, copia l'URL completo della pagina in cui arrivi "
        "e, **entro 5 minuti**, rilancia questo workflow incollandolo nel campo."
    )
    sys.exit(0)

# ---- Passo B: scambia il codice con il refresh token
if PASTED.startswith("http"):
    code = parse_qs(urlparse(PASTED).query).get("code", [""])[0]
else:
    code = unquote(PASTED)
if not code:
    summary("## ❌ Nell'URL incollato non c'è il codice\n\nAssicurati di copiare l'URL "
            "della pagina finale di eBay, quella dopo aver premuto «Accetto».")
    sys.exit(1)

basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
r = requests.post(
    "https://api.ebay.com/identity/v1/oauth2/token",
    headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    data={"grant_type": "authorization_code", "code": code, "redirect_uri": RUNAME},
    timeout=30,
)
if not r.ok:
    summary(f"## ❌ eBay ha rifiutato il codice ({r.status_code})\n\n"
            "Il codice scade dopo circa 5 minuti ed è usabile una volta sola: "
            "rilancia il workflow col campo vuoto per avere un nuovo link e riprova.\n\n"
            f"```\n{r.text[:500]}\n```")
    sys.exit(1)

data = r.json()
refresh_token = data["refresh_token"]
print(f"::add-mask::{refresh_token}")

subprocess.run(
    ["gh", "secret", "set", "EBAY_REFRESH_TOKEN", "--repo", REPO],
    input=refresh_token, text=True, check=True,
)

days = int(data.get("refresh_token_expires_in", 0)) // 86400
summary(f"## ✅ Account eBay collegato!\n\nIl collegamento è valido per circa **{days} giorni**. "
        "Ora puoi avviare il workflow «Controlla vendite eBay».")
