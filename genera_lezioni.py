import json
import os
import re
import sys
import time
from datetime import date

import requests

MODELLI = ["gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
MODALITA = ["responseJsonSchema", "responseSchema", None]
MAX_LEZIONI = int(os.environ.get("MAX_LEZIONI") or 5)
PAUSA = 20


class LimiteRaggiunto(Exception):
    pass


def leggi_testo(percorso):
    with open(percorso, encoding="utf-8") as f:
        return f.read()


CURRICULUM = json.loads(leggi_testo("curriculum.json"))
SISTEMA = leggi_testo("prompt_sistema.txt")
PROMPT_MODULO = leggi_testo("prompt_modulo.txt")
SCHEMA = json.loads(leggi_testo("schema_lezione.json"))
CONTO_429 = 0
FUNZIONA = None


def costruisci_prompt(m):
    prima = "\n".join(f"- Modulo {x['id']}: {x['titolo']}" for x in CURRICULUM if x["id"] < m["id"])
    dopo = "\n".join(f"- Modulo {x['id']}: {x['titolo']}" for x in CURRICULUM if x["id"] > m["id"])
    return (PROMPT_MODULO
            .replace("{ID}", str(m["id"]))
            .replace("{TOTALE}", str(len(CURRICULUM)))
            .replace("{AREA}", m["area"])
            .replace("{TITOLO}", m["titolo"])
            .replace("{CONCETTI}", ", ".join(m["concetti"]))
            .replace("{CASO}", m["caso"])
            .replace("{PRECEDENTI}", prima or "- (nessuno: è il primo modulo)")
            .replace("{SUCCESSIVI}", dopo or "- (nessuno)"))


def chiama(modello, modalita, prompt, chiave):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
    config = {"responseMimeType": "application/json", "temperature": 0.5, "maxOutputTokens": 30000}
    testo = prompt
    if modalita == "responseJsonSchema":
        config["responseJsonSchema"] = SCHEMA
    elif modalita == "responseSchema":
        config["responseSchema"] = SCHEMA
    else:
        testo += ("\n\nRispondi SOLO con un oggetto JSON valido che rispetti esattamente questo schema:\n"
                  + json.dumps(SCHEMA, ensure_ascii=False))
    corpo = {
        "systemInstruction": {"parts": [{"text": SISTEMA}]},
        "contents": [{"role": "user", "parts": [{"text": testo}]}],
        "generationConfig": config,
    }
    return requests.post(url, headers={"x-goog-api-key": chiave, "Content-Type": "application/json"},
                         json=corpo, timeout=300)


def estrai(dati):
    try:
        parti = dati["candidates"][0]["content"]["parts"]
        testo = "".join(p.get("text", "") for p in parti).strip()
        testo = re.sub(r"^```(?:json)?\s*|\s*```$", "", testo).strip()
        return json.loads(testo)
    except Exception as errore:
        print("Impossibile leggere la risposta:", errore)
        return None


def controlla(lezione):
    try:
        for parte in ["titolo", "concetto", "causa_effetto", "caso_studio", "quiz", "simulazione", "riepilogo"]:
            if parte not in lezione:
                return f"manca la parte '{parte}'"
        if len(lezione["quiz"]) != 4:
            return "il quiz non ha 4 domande"
        for q in lezione["quiz"]:
            if len(q["opzioni"]) != 4:
                return "una domanda non ha 4 opzioni"
            if q["indice_corretta"] not in (0, 1, 2, 3):
                return "indice della risposta corretta non valido"
        if len(lezione["concetto"]["glossario"]) < 2:
            return "glossario troppo corto"
        if len(lezione["concetto"]["sezioni"]) < 2:
            return "troppe poche sezioni di teoria"
    except Exception as errore:
        return f"struttura non valida: {errore}"
    return None


def genera(m, chiave):
    global CONTO_429, FUNZIONA
    prompt = costruisci_prompt(m)
    combinazioni = [(a, b) for a in MODELLI for b in MODALITA]
    if FUNZIONA in combinazioni:
        combinazioni.remove(FUNZIONA)
        combinazioni.insert(0, FUNZIONA)
    for modello, modalita in combinazioni:
        for tentativo in range(3):
            try:
                r = chiama(modello, modalita, prompt, chiave)
            except Exception as errore:
                print(f"[{modello}/{modalita}] errore di rete: {errore}")
                time.sleep(20)
                continue
            if r.status_code == 200:
                lezione = estrai(r.json())
                problema = controlla(lezione) if lezione else "JSON non valido"
                if not problema:
                    FUNZIONA = (modello, modalita)
                    return lezione, modello
                print(f"[{modello}/{modalita}] lezione scartata: {problema}")
                continue
            print(f"[{modello}/{modalita}] errore {r.status_code}: {r.text[:300]}")
            if r.status_code == 429:
                CONTO_429 += 1
                if CONTO_429 >= 4:
                    raise LimiteRaggiunto()
                time.sleep(60)
                continue
            if r.status_code in (500, 503):
                time.sleep(30)
                continue
            break
    return None, None


def main():
    chiave = os.environ.get("GEMINI_API_KEY")
    if not chiave:
        sys.exit("Manca la chiave GEMINI_API_KEY.")
    os.makedirs("docs/lezioni", exist_ok=True)
    fatte = 0
    fallite = 0
    for m in CURRICULUM:
        percorso = f"docs/lezioni/{m['id']:02d}.json"
        if os.path.exists(percorso):
            continue
        if fatte >= MAX_LEZIONI:
            break
        print(f"--- Genero il modulo {m['id']}: {m['titolo']}")
        try:
            lezione, modello = genera(m, chiave)
        except LimiteRaggiunto:
            print("Limite gratuito di Google raggiunto: mi fermo, riprova più tardi.")
            break
        if not lezione:
            print(f"Modulo {m['id']} non riuscito: mi fermo.")
            fallite += 1
            break
        lezione["modulo"] = m["id"]
        lezione["area"] = m["area"]
        lezione["generata_il"] = date.today().isoformat()
        lezione["modello"] = modello
        with open(percorso, "w", encoding="utf-8") as f:
            json.dump(lezione, f, ensure_ascii=False, indent=1)
        print(f"Modulo {m['id']} salvato (modello: {modello})")
        fatte += 1
        time.sleep(PAUSA)

    indice = []
    for m in CURRICULUM:
        indice.append({"id": m["id"], "area": m["area"], "titolo": m["titolo"],
                       "disponibile": os.path.exists(f"docs/lezioni/{m['id']:02d}.json")})
    with open("docs/indice.json", "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=1)
    print(f"Fatto: {fatte} nuove lezioni.")
    if fallite and fatte == 0:
        sys.exit(1)


main()
