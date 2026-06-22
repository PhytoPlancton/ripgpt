# Déploiement public — ripgpt

> ⚠️ **À lire avant** : ripgpt pilote un **navigateur headless** contre ChatGPT (protégé
> Cloudflare), avec un cookie minté sur l'IP du Mac. Sur une **IP datacenter (EDJ Labs)**,
> Cloudflare bloque probablement le navigateur et l'usage du cookie peut **flagger le
> compte**. Deux chemins :
>
> - **A — recommandé (fiable)** : navigateur **sur le Mac** + **tunnel Cloudflare** → `ripgpt.nmt.ovh`.
> - **B — pipeline EDJ Labs** : à tester (peut être bloqué par Cloudflare). Détaillé ci-dessous.
>
> Dans les deux cas, l'API est **décorrélée du front** : `ripgpt.nmt.ovh` = API seule
> (la console `/` vient avec). Si tu veux Open WebUI public, c'est un **stack séparé**
> (`chat.nmt.ovh`) qui pointe sur l'API.

---

## Chemin A — Tunnel Cloudflare depuis le Mac (recommandé)

```bash
brew install cloudflared
cloudflared tunnel login                      # autorise nmt.ovh (une fois)
cloudflared tunnel create ripgpt
cloudflared tunnel route dns ripgpt ripgpt.nmt.ovh
cloudflared tunnel run --url http://localhost:8850 ripgpt
```
→ `https://ripgpt.nmt.ovh` pointe sur ripgpt qui tourne sur le Mac. Mets une **clé API forte**
(voir plus bas) ; le Mac doit rester allumé. (Pour un test rapide jetable :
`cloudflared tunnel --url http://localhost:8850` donne une URL `*.trycloudflare.com`.)

---

## Chemin B — EDJ Labs (GHCR → stack → Traefik)

### 1. Image
Le workflow `.github/workflows/build.yml` build sur **tag** → `ghcr.io/phytoplancton/ripgpt`.
Déclenche avec `./deploy.sh "message"` (ou `Cpt`). Rends le package **public** sur GHCR
(github.com/users/PhytoPlancton/packages → ripgpt → settings → Public) si pas de credential registry.

### 2. DNS Cloudflare
`dash.cloudflare.com → nmt.ovh → DNS → Add record` :
`A` · name `ripgpt` · IPv4 `79.137.79.153` · **Proxy : DNS only (gris)** au début.

### 3. Stack EDJ Labs
- **Service Name** : `web`
- **Image** : `ghcr.io/phytoplancton/ripgpt:latest`
- **Ports** : VIDE · **Volumes** : VIDE · **Networks** : `traefik-public`
- **Environment Variables** (secrets ici, jamais dans le repo) :
  ```
  API_KEY=<openssl rand -hex 32>
  CHATGPT_COOKIES=<ta chaîne de cookies complète>
  INJECT_CF_COOKIES=false
  HEADLESS=true
  ANSWER_TIMEOUT=300
  ```
- **Deploy Labels** (PAS "Labels") — remplace `NOM-COMPLET-DU-STACK` (nom + suffixe UUID)
  et note le **port 8850** :
  ```
  traefik.enable                                                       = true
  traefik.docker.network                                               = traefik-public
  traefik.http.routers.NOM-COMPLET-DU-STACK.rule                       = Host(`ripgpt.nmt.ovh`)
  traefik.http.routers.NOM-COMPLET-DU-STACK.entrypoints                = websecure
  traefik.http.routers.NOM-COMPLET-DU-STACK.tls.certresolver           = letsencrypt
  traefik.http.services.NOM-COMPLET-DU-STACK.loadbalancer.server.port  = 8850
  traefik.http.routers.NOM-COMPLET-DU-STACK-http.rule                  = Host(`ripgpt.nmt.ovh`)
  traefik.http.routers.NOM-COMPLET-DU-STACK-http.entrypoints           = web
  traefik.http.middlewares.redirect-to-https.redirectscheme.scheme     = https
  traefik.http.routers.NOM-COMPLET-DU-STACK-http.middlewares           = redirect-to-https
  ```
- **Global Networks** : `traefik-public` / overlay / external: true → Create Stack.

### 4. Déployer & tester
`Cpt` → workflow vert (~3-5 min) → **Update** le stack → attendre 30s →
```bash
curl https://ripgpt.nmt.ovh/v1/models -H "Authorization: Bearer <API_KEY>"
```
Si **vide / 403 / logs "composer timeout" ou Cloudflare** → c'est le blocage datacenter :
bascule sur le **Chemin A** (tunnel depuis le Mac).

---

## Clé API forte (obligatoire en public)
```bash
openssl rand -hex 32
```
→ `API_KEY` (env EDJ Labs ou `.env` local). Les clients envoient `Authorization: Bearer <clé>`.
**Quiconque a la clé tape sur ton compte ChatGPT** (navigateur unique, rate-limité) — garde-la secrète, débit calme.
