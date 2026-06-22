# ripgpt — todo

## État actuel (2026-06-22)
- ✅ Proxy OpenAI-compatible réparé (archi WebSocket 2025) — `/v1/chat/completions`, `/v1/completions`, `/v1/models`.
- ✅ Auth par cookie de session (`CHATGPT_COOKIES` dans `.env`, jamais commit).
- ✅ Modèles sélectionnables + `auto` (override du champ `model` dans la requête sortante).
- ✅ Open WebUI (http://localhost:3001).
- ✅ Console de monitoring (http://localhost:8850/) : santé, latence/modèle, requêtes récentes, sphère Three.js (suit le curseur sur toute la page).
- ✅ Repo privé GitHub : PhytoPlancton/ripgpt. Commits anonymes (pas de nom, pas de co-author).

## En cours — rendre l'API publique (décorrélée du front)
DÉCISION À TRANCHER (1 question, voir plus bas) : où tourne le navigateur headless ?

### ⚠️ Le point critique propre à ripgpt
ripgpt pilote un **vrai navigateur** contre ChatGPT protégé par Cloudflare, avec un cookie
minté sur l'IP résidentielle du Mac. Déployé tel quel sur **EDJ Labs (IP datacenter)** :
- Cloudflare challenge/bloque très probablement le navigateur headless depuis une IP datacenter.
- Utiliser le cookie ChatGPT depuis un datacenter peut **flagger le compte**.

### Chemin A — recommandé : navigateur sur le Mac + tunnel Cloudflare → `ripgpt.nmt.ovh`
- Le navigateur reste là où il marche (IP résidentielle, vrai cookie).
- `cloudflared` expose `localhost:8850` sur `ripgpt.nmt.ovh` via Cloudflare (domaine déjà chez eux).
- API publique, décorrélée, sur le domaine — sans bouger le navigateur fragile.
- Contrainte : le Mac doit rester allumé.

### Chemin B — pipeline EDJ Labs (GHCR → stack → Traefik → `ripgpt.nmt.ovh`)
- Colle parfaitement la stack habituelle, mais **risque Cloudflare/flag** (à tester).
- Si ça passe : 100% dans EDJ Labs. Sinon : repli sur le chemin A.
- Port Traefik = **8850** (pas 3000). Labels + env dans `DEPLOY.md`.

## Pré-requis communs avant exposition publique
- [ ] Clé API **forte** (`openssl rand -hex 32`) — `API_KEY` en env (jamais en clair dans le repo).
- [ ] `CHATGPT_COOKIES` en env du service (EDJ Labs ou `.env` local), jamais commit.
- [ ] Confirmer le chemin (A ou B).

## Next steps
1. Trancher A vs B.
2. (B) `Cpt` → build image GHCR `ghcr.io/phytoplancton/ripgpt` → créer stack EDJ Labs (voir DEPLOY.md) → DNS Cloudflare `ripgpt` → 79.137.79.153 (gris) → tester.
3. (A) installer `cloudflared`, tunnel `ripgpt.nmt.ovh` → `localhost:8850`, clé forte.
4. Vérifier : `curl https://ripgpt.nmt.ovh/v1/models` avec la clé.
