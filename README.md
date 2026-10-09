# Local signature clean-up validation

Install the public dependencies, put source image files in `input/`, then run:

```powershell
py -m pip install -r requirements.txt
py signature_validator.py --input input --output output --report-dir reports
```

The script writes one cleaned `*_ephoto.png` per source plus `signature_report.json`
and `signature_report.csv`. It uses illumination normalisation and thresholding to
remove paper/shadows, performs no morphology on ink, crops with a margin, and places
the result on a 521×134 (4:1) PNG canvas without non-uniform scaling.

Both contrast directions are accepted: a white or pale pen on a dark background is
detected and inverted before extraction, so it goes through exactly the same pipeline
and is exported as black ink on white like any other sheet. Such files are marked
`+inverted` in the report's `processing_mode` column.

BioGaze is not available from PyPI. If the client supplies its vendor wheel/private
index, install it separately; `--require-biogaze` then makes validation stop unless
the module is available. Its public Python processing API is not stable enough to
safely invoke by name, so the deterministic OpenCV path performs the pixel work.

The validation checks are technical local heuristics. They cannot certify ANTS or
certif-idphoto.fr acceptance; test an exported file in the actual intake system
before making that claim. Adjust the service’s actual file limit with `--max-bytes`.

## Microservice de dossiers : photo ANTS + signature + validation humaine

Le service reçoit un dossier client depuis Make.com, prépare **la photo d'identité** et
**la signature**, calcule un rapport de conformité, puis attend la décision d'un
contrôleur avant de transmettre le dossier à Make/Ephoto.io.

```
WooCommerce → Webhook Make → POST /api/v1/ingest → traitement OpenCV → file d'attente
                                                          ↓
                                        /admin/dashboard (contrôleur, mot de passe)
                                                          ↓
                          « Accepter » → webhook sortant Make/Ephoto.io → archivage
                          « Refuser »  → motif enregistré, rien n'est transmis
```

### Arborescence

```
Ephoto/
├─ signature_validator.py      # pipeline signature (nettoyage, orientation, rapport)
├─ app.py                      # page publique de contrôle signature (liste blanche de fichiers)
├─ index.html
├─ service/                    # le microservice
│  ├─ main.py                  # assemblage : middlewares, routes, cycle de vie
│  ├─ api.py                   # routes machine : ingest, statut, validate, fichiers, santé
│  ├─ admin.py                 # panneau de contrôle (HTML) et actions du contrôleur
│  ├─ workflow.py              # les règles : réception, traitement, décision, rétention
│  ├─ jobs.py                  # pool borné qui exécute les traitements d'images
│  ├─ web.py                   # en-têtes de sécurité, anti-CSRF, taille max, journal
│  ├─ config.py                # variables d'environnement (secrets « fail closed »)
│  ├─ database.py              # SQLite : dossiers, migrations, historique d'audit
│  ├─ storage.py               # images sur disque (écriture atomique)
│  ├─ security.py              # clés d'API, HTTP Basic, limiteur d'échecs
│  ├─ outbound.py              # webhook sortant + téléchargements protégés (SSRF)
│  ├─ models.py                # Check / ProcessedImage, calcul du score
│  ├─ processing/              # photo ANTS, adaptateur signature, décodage
│  ├─ templates/               # Jinja2 (dashboard, fiche dossier, erreurs)
│  ├─ static/                  # CSS compilé + JS du panneau (servis sous /admin/static)
│  └─ assets/                  # sources Tailwind (non servies)
├─ tests/smoke_test.py         # test bout en bout, sans réseau ni données réelles
├─ tests/unit_test.py          # migrations, verrous, SSRF, orientation sur les exemples
├─ wordpress-plugin/           # extension WooCommerce de contrôle (voir son README)
└─ storage/                    # dossiers en attente (hors dépôt, purgé automatiquement)
```

### Cycle de vie d'un dossier

```
processing ──(traitement)──► pending ──accepter──► accepted ──(rétention)──► purgé
    │                          └─────refuser───► rejected ──(rétention)──► purgé
    └────────(échec)─────────► error ──────────────────────(rétention)──► purgé
```

- Les originaux sont écrits sur disque **avant** la réponse 202 ; le traitement tourne
  ensuite sur un pool borné (`PROCESSING_WORKERS`). Un dossier encore `processing` au
  redémarrage est relancé automatiquement.
- Chaque changement d'état est une mise à jour conditionnelle : deux acceptations
  simultanées (panneau + WordPress) ne transmettent jamais deux fois, et un refus reçu
  pendant le traitement n'est plus annulé à la fin de celui-ci.
- Un verrou par dossier empêche un recadrage pendant une transmission. S'il reste posé
  après un crash, le démarrage le libère et le signale sur la fiche.
- Un renvoi des **mêmes fichiers pour la même commande** (relance Make, double chemin
  d'entrée) renvoie le dossier existant (`200`, `duplicate: true`) au lieu d'en créer un.
- Chaque étape est inscrite dans l'**historique** du dossier (fiche, colonne de droite).

### Points d'entrée

| Méthode | Route | Auth | Rôle |
|---|---|---|---|
| POST | `/api/v1/ingest` | clé d'intake | Réception : JSON (base64 ou URL) **ou** multipart. `202` nouveau, `200` doublon |
| GET | `/api/v1/submissions/{id}` | clé d'intake ou de contrôle | Statut, rapports, `error`, `accept_ready` |
| POST | `/api/v1/validate/{id}` | Basic ou clé de contrôle | Décision `{"action": "accept" \| "reject", "reason": "…"}` |
| GET | `/api/v1/files/{id}/{kind}` | Basic ou clé de contrôle | Images (original / préparée) |
| POST | `/api/v1/submissions/{id}/recrop` | Basic ou clé de contrôle | `zoom` 0,5–2 ; `dx`, `dy` −0,5–0,5 (fractions du cadre) |
| POST | `/api/v1/submissions/{id}/rotate-signature` | Basic ou clé de contrôle | `rotation` en degrés, sens horaire |
| GET | `/admin/dashboard` | Basic | Liste des dossiers, scores, filtres |
| GET/POST | `/admin/nouveau` | Basic | Création manuelle d'un dossier (test ou comptoir) |
| GET | `/admin/submissions/{id}` | Basic | Avant/après, checklist, historique, décision |
| GET | `/api/health` | — | `ok` / `degraded` (sans MediaPipe) / `error` (503, base illisible) |

**Clés.** Elles passent **uniquement** dans l'en-tête `X-API-Key` (plus jamais dans l'URL,
où elles finissaient dans les journaux et le navigateur). La clé d'intake
(`INGEST_API_KEY`, celle de Make) ne permet que de déposer et lire un dossier ; la clé de
contrôle (`REVIEW_API_KEY`, celle du plugin WordPress) permet d'agir comme contrôleur,
avec le nom de l'utilisateur dans `X-Reviewer-User`. Un champ `reviewer` dans le corps
est ignoré : la décision est attribuée à l'identité authentifiée.

**Acceptation.** Refusée (`409`) tant que le dossier n'est pas `pending`, sans erreur,
avec photo et signature préparées, et sans opération en cours — la même règle que le
bouton du panneau. `502` si Make refuse : le dossier reste dans la file. Le webhook
sortant porte `Idempotency-Key: <submission_id>` pour que Make puisse ignorer un renvoi.

La page publique de contrôle de signature reste montée à la racine et garde exactement
son comportement actuel.

### Orientation de la signature

Seule une signature **verticale** (feuille photographiée de côté) est tournée
automatiquement, de 90° ou 270°. Le service ne redresse plus automatiquement les angles
libres — l'inclinaison fait partie de la signature, et le redressement par ACP tournait
des signatures droites de 225° ou les retournait — et ne retourne jamais seul une
signature horizontale de 180° : si l'heuristique le suggère, la signature est gardée
telle quelle et marquée « à confirmer ». Le contrôleur peut appliquer n'importe quel
angle depuis la fiche ; le recalcul repart toujours de l'original.

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `INGEST_API_KEY` | — | **Obligatoire** : sans elle `/api/v1/ingest` répond 503 |
| `REVIEW_API_KEY` | — | Clé du plugin WordPress pour les actions de contrôle ; sans elle, seul le Basic fonctionne |
| `ADMIN_USER` / `ADMIN_PASSWORD` | — | **Obligatoires** : sans eux le panneau répond 503 |
| `MAKE_WEBHOOK_URL` | — | Webhook de sortie (HTTPS) ; sans lui l'acceptation est indisponible |
| `MAKE_WEBHOOK_TOKEN` | — | Envoyé en `Authorization: Bearer …` — à vérifier dans le scénario B |
| `STORAGE_DIR` | `/data` (Docker) | Images et base SQLite |
| `PURGE_AFTER_DAYS` | `30` | Purge des dossiers décidés (RGPD) |
| `PURGE_OPEN_AFTER_DAYS` | `0` (jamais) | Purge des dossiers jamais décidés |
| `FLATTEN_BACKGROUND` | `auto` | `always`, `auto` ou `never` pour le détourage du fond |
| `PUBLIC_BASE_URL` | — | Préfixe du lien de contrôle ; sert aussi au contrôle d'origine (CSRF) |
| `PROCESSING_WORKERS` | `2` | Images traitées en parallèle (≈ 300 Mo chacune au pire) |
| `FETCH_ALLOWED_HOSTS` | — | Domaines autorisés pour les URL d'images (ex. `boutique.fr`) ; recommandé |
| `MAX_UPLOAD_BYTES` / `MAX_IMAGE_PIXELS` | 15 Mo / 60 Mpx | Refus avant décodage (bombes de décompression) |
| `REQUIRE_FACE_MESH` | `true` (Docker) | `/api/health` passe à `degraded` sans MediaPipe |
| `LOG_LEVEL` | `INFO` | Journal sur stdout, sans chaîne de requête ni donnée client |

### Sécurité

- La page publique ne sert plus que `index.html`, `input/`, `output/` et `reports/`.
  Auparavant **tout le dépôt** était téléchargeable (code, Dockerfile, portraits de test).
- Panneau : CSP stricte (aucun script tiers ni en ligne), `X-Frame-Options: DENY`,
  `Cache-Control: no-store` sur le panneau et les images, contrôle d'origine sur tout
  POST (les identifiants Basic sont rejoués par le navigateur sur un formulaire forgé).
- Téléchargements d'images : HTTP(S) uniquement, adresses privées/locales refusées à
  chaque redirection, taille et type vérifiés.
- 10 échecs d'authentification en 5 minutes bloquent l'adresse (429).

### Exploitation

- **Un seul worker uvicorn** : le pool de traitement et le limiteur vivent en mémoire.
- **Dépendances verrouillées** : `constraints.txt` fixe chaque version. Après modification
  de `requirements.txt`, régénérez-le pour linux/CPython 3.12 :
  `pip install --dry-run --ignore-installed --report r.json --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --python-version 3.12 --only-binary=:all: --target t -r requirements.txt`
  puis reportez les versions de `r.json`. Le build Docker échoue si OpenCV 4, NumPy 1 et
  MediaPipe ne se chargent pas ensemble.
- **Styles du panneau** : après un changement de classes dans les gabarits,
  `npx tailwindcss@3.4.17 -c service/assets/tailwind.config.js -i service/assets/admin.src.css -o service/static/admin.css --minify`
  (la CI vérifie que le fichier est à jour).
- Les migrations de la base s'appliquent seules au démarrage (`PRAGMA user_version`).

### Contrôles de la photo

Géométrie 35 × 45 mm : hauteur de tête 70–80 % du cadre, ligne des yeux à 33–50 % du
haut, export au ratio 414 × 532 (ou 828 × 1064) en JPEG sous 2 Mo. S'y ajoutent
l'inclinaison de la tête, les yeux ouverts, la bouche fermée, la visibilité des oreilles,
l'uniformité et la clarté du fond, l'exposition, la netteté et la présence d'un seul
visage.

**Détourage conditionnel.** Une photo de studio dont le fond est déjà conforme est
conservée telle quelle : le service n'applique plus de filtre de contraste local ni de
détourage par défaut. Si le fond doit être remplacé, le sujet est reposé sur le gris
verrouillé **`#d4d7d3`**. La couleur est imposée par le service : « le fond doit être uni,
de couleur claire (bleu clair ou gris clair par exemple). **Le fond blanc est interdit** »
([service-public.fr F10619](https://www.service-public.gouv.fr/particuliers/vosdroits/F10619)),
d'où un `UNIFORM_BACKGROUND_HEX` unique et un contrôle de fond borné des deux côtés. La
segmentation vient de **MediaPipe Selfie Segmentation** ; GrabCut ne sert plus que de
repli, et une garde annule le détourage plutôt que d'entamer le visage.

**Exposition.** Notée sur l'écrêtage et le contraste du visage, pas sur sa clarté absolue :
la carnation couvre légitimement toute l'échelle, et un seuil absolu refuserait une peau
foncée pour sa seule couleur. Le texte demande une photo « ni surexposée ni sous-exposée »
et « correctement contrastée ».

**Oreilles.** Avec Face Mesh, une vérification prudente compare les zones attendues des
deux oreilles à la couleur de joue de la personne et confirme que le recadrage ne les a
pas coupées. Sans Face Mesh, le statut reste « indéterminé » : le repli Haar ne permet pas
de présenter ce critère comme conforme.

Les repères viennent de **MediaPipe Face Mesh**, désormais une dépendance ferme ; les
cascades de Haar fournies par OpenCV ne subsistent que comme repli de développement, sans
détourage ni mesure de la bouche. Un critère que le détecteur disponible ne sait pas
mesurer est affiché **« indéterminé » (pastille grise)** et exclu du score : il n'est jamais
présenté comme conforme. `/api/health` indique le détecteur réellement actif.

### Lancer et tester en local

```bash
py -m pip install -r requirements.txt -c constraints.txt
py tests/unit_test.py
py tests/smoke_test.py
```

```bash
INGEST_API_KEY=dev ADMIN_USER=ctrl ADMIN_PASSWORD=ctrl py -m uvicorn service.main:app --reload
```

### Limites connues

- Les seuils (netteté, uniformité du fond, ouverture des yeux) sont des heuristiques
  locales : elles ne valent pas acceptation par l'ANTS, d'où le contrôle humain.
- Le détourage est abandonné, et la photo d'origine conservée, si la découpe entame le
  visage. Sans MediaPipe le repli GrabCut échoue à cette garde sur la plupart des
  portraits : le service ne détoure alors pas, et `background_ok` le signale au contrôleur.
- L'estimation du sommet du crâne est anthropométrique (les repères s'arrêtent au front) :
  c'est une approximation, que le contrôleur peut corriger avec le recadrage manuel.
- `opencv-python-headless` est épinglé sous 5.0 : OpenCV 5 a supprimé `CascadeClassifier`
  et ses cascades, donc sans MediaPipe aucune géométrie ne serait mesurable.

## Déploiement Dokploy : vraie API Python

Le dépôt contient une application FastAPI qui sert l’interface et traite les uploads
sur le serveur avec `signature_validator.py`, OpenCV et Pillow. Les fichiers uploadés
sont stockés dans un dossier temporaire, supprimé dès que la réponse PNG/JSON est
renvoyée.

L’interface accepte jusqu’à 20 images à la fois. Chaque image est traitée par le
pipeline Python et le navigateur affiche immédiatement les cartes avant/après, les
cinq contrôles et le score du nouveau lot. Les résultats d’upload ne sont pas ajoutés
aux dossiers publics `input/`, `output/` ou `reports/`.

1. Poussez ce dépôt sur GitHub.
2. Dans Dokploy, créez un service **Docker Compose** connecté au dépôt et à la branche
   `main`.
3. Indiquez `./docker-compose.yml` comme chemin Compose.
4. Dans l’onglet **Domains**, ajoutez votre domaine et sélectionnez le port interne
   `8000`.
5. Dans l’onglet **Environment**, renseignez au minimum `INGEST_API_KEY`,
   `REVIEW_API_KEY` (si le plugin WordPress est utilisé), `ADMIN_USER`, `ADMIN_PASSWORD`,
   `MAKE_WEBHOOK_URL` et `PUBLIC_BASE_URL` (voir le tableau plus haut). Sans ces
   variables, l’ingestion et le panneau de contrôle répondent 503.
6. Cliquez sur Deploy. Les pushes futurs sur la branche choisie peuvent déclencher le
   redéploiement automatique.

Le conteneur démarre `service.main:app` : le microservice et la page publique de
signature sont servis par le même processus. Les dossiers clients vivent dans le volume
`submissions` monté sur `/data`, hors de l’arborescence servie en statique.

Dokploy recommande sa configuration de domaine native plutôt que des labels Traefik
écrits à la main. Pour une démo, n’exposez dans `input/` et `output/` que des
échantillons autorisés ou anonymisés.
