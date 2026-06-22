# Leçons (relire au démarrage de chaque session)

Format : `[date] | ce qui a mal tourné | règle pour l'éviter`

## Conventions (toujours)
- `[2026-06-22] | — | Commits 100% ANONYMES : jamais de nom/prénom, jamais "Co-Authored-By: Claude". Auteur git = PhytoPlancton <PhytoPlancton@users.noreply.github.com>.`
- `[2026-06-22] | — | Raccourcis : "cp" = commit+push · "Cpt" = commit+push+tag (déclenche le déploiement GHCR).`
- `[2026-06-22] | — | Secrets jamais en clair / jamais commités. CHATGPT_COOKIES & API_KEY → env runtime uniquement (.env local gitignored, ou env EDJ Labs). Toujours un garde-fou "pas de .env stagé" avant un commit.`
- `[2026-06-22] | — | DB : toujours fermer les connexions (limite 500 simultanées, plusieurs apps). Réutiliser un pool, fermer en finally.`

## ripgpt — comportement de ChatGPT
- `[2026-06-22] | La réponse ne revient plus sur le POST /backend-api/f/conversation (handoff) | Les tokens arrivent par WebSocket (wss://ws.chatgpt.com), encodés v1 dans les frames. Hooker fetch ET WebSocket.`
- `[2026-06-22] | Forcer le modèle via ?model= dans l'URL ne marche pas (param ignoré) | Réécrire le champ "model" de la requête sortante dans l'intercepteur fetch (fiable). Vérifié via le model_slug renvoyé.`
- `[2026-06-22] | Health-check de session trop agressif (goto+evaluate) wedgeait le navigateur toutes les ~15 min | is_alive() ne doit JAMAIS naviguer ; en cas d'erreur transitoire → considérer la session vivante.`
- `[2026-06-22] | Matraquer le navigateur (rafale de requêtes) déclenche l'anti-abus ChatGPT → réponses vides / composer timeout | Débit séquentiel calme. En cas de wedge : docker compose restart api.`

## Infra / Docker
- `[2026-06-22] | Disque Mac plein à 100% → VM Docker corrompue (EXT4 "input/output error"), moteur bloqué | Garder de l'espace hôte. Récup : libérer de l'espace puis QUITTER Docker Desktop par son vrai nom ("Docker Desktop") + kill -9 des résidus, puis reopen (sinon le superviseur relance l'ancien moteur bloqué).`
- `[2026-06-22] | Image buildée pendant que le disque se remplissait → fichiers corrompus dans l'image (SyntaxError null bytes) | Après incident disque, rebuild --no-cache.`
- `[2026-06-22] | OpenWebUI sature le navigateur unique avec ses appels annexes (titre/tags/autocomplete) | Désactiver ENABLE_TITLE_GENERATION / TAGS / AUTOCOMPLETE / RETRIEVAL_QUERY / EVALUATION_ARENA_MODELS.`
- `[2026-06-22] | Port 3000 déjà pris sur la machine | Port configurable (OPENWEBUI_PORT, défaut 3001).`

## Déploiement (EDJ Labs / Cloudflare)
- `[2026-06-22] | — | ripgpt pilote un navigateur contre Cloudflare : déployé sur IP datacenter (EDJ Labs), Cloudflare bloque probablement + risque de flag du compte ChatGPT. Préférer : navigateur sur le Mac + tunnel Cloudflare. Sinon tester EDJ Labs avant de s'engager.`
- `[2026-06-22] | — | Traefik EDJ Labs : utiliser "Deploy Labels" (pas "Labels"), port loadbalancer = 8850 pour ripgpt, DNS Cloudflare en gris (DNS only) au début.`
- `[2026-06-22] | Build GHCR échoué en ~15s : nom d'image avec majuscule (ghcr.io/PhytoPlancton/...) refusé | GHCR exige des minuscules → IMAGE="ghcr.io/$(echo '\${{ github.repository }}' | tr '[:upper:]' '[:lower:]')". Aussi bumper actions/checkout@v4 + docker/login-action@v3 (Node20 déprécié).`
