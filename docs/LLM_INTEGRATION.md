# ripgpt — Guide d'intégration API (pour un agent / une IA implémenteur)

> Tu es une IA qui doit intégrer des fonctionnalités d'IA en appelant **ripgpt**.
> ripgpt est une **API compatible OpenAI** qui pilote ChatGPT (navigateur) en coulisses.
> C'est **gratuit** mais il y a **un seul navigateur partagé** → conçois ton intégration
> pour envoyer les requêtes **en série**, avec des timeouts généreux et un backoff sur 429/503.

---

## 1. Essentiel (TL;DR)

- **Base URL** : `https://ripgpt.nmt.ovh/v1`
- **Auth** : header `Authorization: Bearer <TA_CLE>`
- **Compatible OpenAI** : utilise n'importe quel SDK OpenAI en changeant juste `base_url` + `api_key`.
- **Endpoint principal** : `POST /v1/chat/completions`
- **Coût réel** : 0 $ (c'est un proxy navigateur). Un « coût API équivalent » est calculé à titre indicatif.
- **Contrainte n°1** : un seul navigateur → **concurrence = 1**, latence de quelques secondes à quelques minutes.

---

## 2. Authentification

Chaque appel `/v1/*` exige une clé Bearer valide (l'auth est *fail-closed* : pas de clé valide = 401).

```
Authorization: Bearer TA_CLE
```

- Génère/gère les clés dans la console admin : `https://ripgpt.nmt.ovh/` → **API keys** → *create*.
- Stocke la clé dans une **variable d'environnement / secret**. Ne la mets **jamais** en dur dans le code ni dans un dépôt.
- Erreur `401 invalid_api_key` si la clé est absente, invalide ou révoquée.

---

## 3. Endpoints

| Méthode | Chemin | Auth | Rôle |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | ✅ | Chat (à utiliser par défaut) |
| `POST` | `/v1/completions` | ✅ | Complétion texte (legacy) |
| `GET`  | `/v1/models` | ✅ | Liste les modèles **activés** (interroge-le, les modèles peuvent être désactivés) |
| `GET`  | `/health` | ❌ | `{"status":"ok","session_ready":true}` — sonde de disponibilité |

---

## 4. Modèles disponibles

Interroge `GET /v1/models` pour la liste à jour. Repère actuel :

| `model` | Pour quoi | Vitesse | Notes |
|---|---|---|---|
| `auto` | Usage général (route seul vers rapide ou raisonnement) | variable | **Bon défaut** |
| `gpt-5.5` | Meilleure qualité générale | moyenne | |
| `gpt-5.5-instant` | Réponses rapides | rapide | idéal pour du volume simple |
| `gpt-5.5-thinking` | Raisonnement / tâches difficiles | lente | |
| `gpt-5.4-thinking` | Raisonnement moins cher | lente | |
| `gpt-5.3` | Modèle plus ancien | moyenne | |
| `o3` | Raisonnement | lente | |
| `chatgpt` | Chat **persistant** (garde l'historique sur le compte partagé) | moyenne | ⚠️ pas d'isolation entre appelants — éviter en multi-tenant |
| `gpt-image` | Génération / édition d'images | lente | renvoie des **URLs d'images hébergées** |

---

## 5. Format de requête (Chat Completions, style OpenAI)

```json
{
  "model": "auto",
  "messages": [
    {"role": "system", "content": "Tu es un assistant concis."},
    {"role": "user", "content": "Explique la relativité en 2 phrases."}
  ],
  "stream": false
}
```

- `messages[].role` : `system` | `user` | `assistant` (le multi-tour et le system prompt sont supportés).
- `messages[].content` : soit une **string**, soit un **tableau de parts** (`text`, `image_url`, `file` — voir §6-8).
- `stream` : `true` / `false`.
- `stop` : string ou liste de strings.
- `n` : **doit valoir 1** (sinon 400).
- `stream_options.include_usage` : `true` pour recevoir l'usage en fin de flux.

### Paramètres honorés vs ignorés

- ✅ **Honorés** : `model`, `messages`, `stream`, `stop`, `n` (=1), `stream_options.include_usage`.
- ⚪ **Ignorés (no-op, tu peux les envoyer, ils n'ont aucun effet)** : `temperature`, `top_p`, `seed`, `logprobs`, `max_tokens`, `presence_penalty`, `frequency_penalty`, `response_format` (mode JSON).
- ❌ **Non supportés** : `tools` / function calling / tool calls. Pour de la sortie structurée : **demande le JSON dans le prompt** et parse/valide toi-même (avec re-essai si le parse échoue).

---

## 6. Entrée image (vision)

Envoie l'image en **data URL base64** dans une part `image_url` :

```json
{"role": "user", "content": [
  {"type": "text", "text": "Décris cette image."},
  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
]}
```

Le base64 est la voie fiable (ne compte pas sur des URLs http distantes).

---

## 7. Entrée fichier (PDF, txt, docx…)

Deux options :
1. **Petit contenu** : mets simplement le texte dans le message.
2. **Fichier** : part `file` avec le contenu en data URL base64 :

```json
{"role": "user", "content": [
  {"type": "text", "text": "Résume ce document."},
  {"type": "file", "file": {"filename": "rapport.pdf", "file_data": "data:application/pdf;base64,JVBERi0x..."}}
]}
```

Les tours de fichier sont plus lents (comptez jusqu'à quelques minutes) → augmente les timeouts.

---

## 8. Génération / édition d'image

Utilise `model: "gpt-image"`. La réponse (`choices[0].message.content`) contient une ou plusieurs **URLs hébergées** :

```
https://ripgpt.nmt.ovh/images/<id>.png
```

⚠️ **Ces URLs sont éphémères** (expirent au bout de quelques heures). Si tu dois garder l'image, **télécharge-la immédiatement** et ré-héberge-la de ton côté. Pour éditer : envoie l'image source en `image_url` (§6) + une instruction texte, toujours avec `model: "gpt-image"`.

---

## 9. Streaming (SSE)

Mets `stream: true`. Tu reçois des chunks au format OpenAI (`chat.completion.chunk` avec `choices[0].delta.content`), terminés par `data: [DONE]`. Le texte arrive en quasi-temps réel ; pour les tours image/fichier, la réponse est envoyée d'un bloc à la fin.

---

## 10. Réponse & usage

Forme standard OpenAI. `usage.prompt_tokens` / `completion_tokens` sont des **estimations** (tiktoken). Le coût réel est nul ; le coût « équivalent API » est visible dans le dashboard, pas dans la réponse.

---

## 11. Erreurs & comment les gérer (IMPORTANT)

Format : `{"error": {"message": "...", "type": "...", "code": "..."}}`.

| HTTP | `code` | Signification | Que faire |
|---|---|---|---|
| 401 | `invalid_api_key` | Clé absente/invalide/révoquée | Corrige la clé (ne pas réessayer) |
| 404 | `model_not_found` | Modèle inconnu ou désactivé | Interroge `/v1/models` |
| 413 | `payload_too_large` | Corps trop gros (> ~160 Mo) | Réduis la taille du fichier |
| 429 | `rate_limited` | Trop de tentatives (login admin) | Backoff |
| 503 | `overloaded` | File d'attente pleine (header **`Retry-After`**) | **Backoff exponentiel + retry** |
| 503 | `paused` | Proxy mis en pause par l'admin | Réessaie plus tard |
| 500 | `server_error` | Erreur amont (message générique + `ref=…`) | Retry 1×, sinon logguer la `ref` |

**Règle** : sur `429`/`503`, respecte `Retry-After` et fais un backoff exponentiel. Une réponse vide inattendue = transitoire → 1 re-essai.

---

## 12. Limites opérationnelles (dimensionne ton intégration là-dessus)

1. **Un seul navigateur partagé** → les requêtes sont **sérialisées**. Garde **concurrence = 1** (2 max). Ne parallélise pas.
2. **Latence** : `instant` ~3–8 s, modèles `thinking` ~10–60 s, fichiers/images jusqu'à quelques minutes. Mets un **timeout client ≥ 180 s** (≥ 600 s si tu envoies des fichiers/images).
3. **File d'attente plafonnée** → `503 overloaded` + `Retry-After` quand c'est chargé.
4. **Taille du corps ≤ ~160 Mo** (uploads base64).
5. **Confidentialité** : **un seul compte ChatGPT partagé**. N'envoie pas de secrets/PII ; la « mémoire » ChatGPT peut faire fuiter du contexte entre appels. Utilise les modèles en chat **temporaire** (défaut) plutôt que `chatgpt` pour l'isolation.
6. **Disponibilité** : dépend du PC hôte + de la session ChatGPT. Sonde `GET /health` (`session_ready:true`) avant un gros lot.

---

## 13. Exemples de code

### Python — SDK OpenAI
```python
from openai import OpenAI
import os

client = OpenAI(base_url="https://ripgpt.nmt.ovh/v1", api_key=os.environ["RIPGPT_API_KEY"])

r = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explique la relativité en 2 phrases."}],
    timeout=180,
)
print(r.choices[0].message.content)
```

### Python — streaming
```python
stream = client.chat.completions.create(
    model="gpt-5.5-instant",
    messages=[{"role": "user", "content": "Écris un haïku sur la mer."}],
    stream=True, timeout=180,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### Python — retry/backoff (à mettre autour de tous tes appels)
```python
import time

def chat(client, **kw):
    kw.setdefault("timeout", 180)
    for attempt in range(5):
        try:
            return client.chat.completions.create(**kw)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (429, 503):
                time.sleep(min(2 ** attempt, 30))   # backoff exponentiel
                continue
            raise
    raise RuntimeError("ripgpt indisponible (busy)")
```

### Python — vision + image gen
```python
import base64
img = base64.b64encode(open("photo.jpg", "rb").read()).decode()
r = client.chat.completions.create(model="gpt-5.5", messages=[
    {"role": "user", "content": [
        {"type": "text", "text": "Que vois-tu ?"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}},
    ]}], timeout=180)
print(r.choices[0].message.content)

gen = client.chat.completions.create(model="gpt-image",
    messages=[{"role": "user", "content": "un chat astronaute, aquarelle"}], timeout=300)
print(gen.choices[0].message.content)   # -> URL https://ripgpt.nmt.ovh/images/....png (éphémère)
```

### Node — SDK OpenAI
```js
import OpenAI from "openai";
const client = new OpenAI({
  baseURL: "https://ripgpt.nmt.ovh/v1",
  apiKey: process.env.RIPGPT_API_KEY,
  timeout: 180000,
});
const r = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Dis bonjour." }],
});
console.log(r.choices[0].message.content);
```

### curl
```bash
curl https://ripgpt.nmt.ovh/v1/chat/completions \
  -H "Authorization: Bearer $RIPGPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"dis OK"}]}'
```

---

## 14. Réglages recommandés pour ton agent

- **Modèle** : `auto` par défaut ; `gpt-5.5-instant` pour la vitesse ; `gpt-5.5-thinking` pour le raisonnement difficile ; `gpt-image` pour les images.
- **Concurrence** : 1 (file d'attente côté client). Ne lance pas 10 appels en parallèle.
- **Timeout** : 180 s (texte), 600 s (fichiers/images).
- **Robustesse** : backoff exponentiel sur 429/503 (respecte `Retry-After`), 1 re-essai sur réponse vide.
- **Sortie structurée** : demande explicitement le JSON dans le prompt, puis valide/parse (les modes `response_format`/tools ne sont pas disponibles).
- **Ne pas dépendre de** `temperature`, `seed`, `max_tokens` (ignorés).
- **Confidentialité** : pas de secrets dans les prompts ; préfère les modèles temporaires.

---

## 15. Résumé à coller dans le contexte système de ton IA

```
API cible : ripgpt, compatible OpenAI.
- base_url = https://ripgpt.nmt.ovh/v1 ; auth = Bearer <clé env RIPGPT_API_KEY>.
- Endpoint : POST /v1/chat/completions (style OpenAI). Modèles : auto (défaut), gpt-5.5,
  gpt-5.5-instant, gpt-5.5-thinking, gpt-5.4-thinking, gpt-5.3, o3, chatgpt, gpt-image.
- Supporté : multi-tour, system prompt, streaming, vision (image_url base64), fichiers
  (part file base64), génération d'image (model gpt-image -> URL éphémère à télécharger).
- Ignorés : temperature, top_p, seed, max_tokens, response_format. Non supporté : tools/function calling.
  Pour du JSON, l'imposer dans le prompt et parser.
- UN SEUL navigateur partagé : concurrence = 1, latence 3 s–plusieurs minutes,
  timeout >= 180 s (600 s pour fichiers/images), backoff sur 429/503 (Retry-After).
- n=1 obligatoire. Corps <= 160 Mo. Compte ChatGPT partagé -> pas de secrets/PII.
```
