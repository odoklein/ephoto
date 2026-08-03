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
├─ signature_validator.py      # pipeline signature en production — inchangé
├─ app.py                      # page publique de contrôle signature — inchangée
├─ index.html
├─ service/                    # le microservice
│  ├─ main.py                  # FastAPI : ingest, dashboard, validate
│  ├─ config.py                # variables d'environnement (secrets « fail closed »)
│  ├─ database.py              # SQLite : une ligne par dossier
│  ├─ storage.py               # images sur disque (original + traité)
│  ├─ security.py              # clé d'API (Make) + HTTP Basic (contrôleur)
│  ├─ outbound.py              # webhook sortant + téléchargement des sources
│  ├─ models.py                # Check / ProcessedImage, calcul du score
│  ├─ processing/
│  │  ├─ photo_processor.py    # cadrage et contrôles ANTS/ICAO
│  │  ├─ signature_processor.py# adaptateur vers signature_validator.py
│  │  └─ imaging.py            # décodage, encodage, netteté
│  └─ templates/               # Jinja2 + Tailwind (dashboard, fiche dossier)
├─ tests/smoke_test.py         # test bout en bout, sans réseau ni données réelles
└─ storage/                    # dossiers en attente (hors dépôt, purgé automatiquement)
```

### Points d'entrée

| Méthode | Route | Auth | Rôle |
|---|---|---|---|
| POST | `/api/v1/ingest` | `X-API-Key` | Réception Make : JSON (base64 ou URL) **ou** multipart |
| GET | `/api/v1/submissions/{id}` | `X-API-Key` | Statut et rapports, pour un scénario Make en attente |
| POST | `/api/v1/validate/{id}` | Basic | Décision `{"action": "accept" \| "reject", "reason": "…"}` |
| GET | `/admin/dashboard` | Basic | Liste des dossiers, scores, filtres |
| GET/POST | `/admin/nouveau` | Basic | Création manuelle d'un dossier (test ou comptoir) |
| GET | `/admin/submissions/{id}` | Basic | Avant/après, métadonnées, checklist, décision |
| POST | `/admin/submissions/{id}/recrop` | Basic | Recadrage manuel (zoom + décalages) |
| GET | `/api/health` | — | État du service et détecteur de visage actif |

La page publique de contrôle de signature reste montée à la racine et garde exactement
son comportement actuel.

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `INGEST_API_KEY` | — | **Obligatoire** : sans elle `/api/v1/ingest` répond 503 |
| `ADMIN_USER` / `ADMIN_PASSWORD` | — | **Obligatoires** : sans eux le panneau répond 503 |
| `MAKE_WEBHOOK_URL` | — | Webhook de sortie ; sans lui l'acceptation échoue en 502 |
| `MAKE_WEBHOOK_TOKEN` | — | Envoyé en `Authorization: Bearer …` |
| `STORAGE_DIR` | `/data` (Docker) | Images et base SQLite |
| `PURGE_AFTER_DAYS` | `30` | Purge des dossiers décidés (RGPD) |
| `FLATTEN_BACKGROUND` | `auto` | `always`, `auto` ou `never` pour le détourage du fond |
| `PUBLIC_BASE_URL` | — | Préfixe du lien de contrôle renvoyé à Make |

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
py -m pip install -r requirements.txt
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
5. Dans l’onglet **Environment**, renseignez au minimum `INGEST_API_KEY`, `ADMIN_USER`,
   `ADMIN_PASSWORD`, `MAKE_WEBHOOK_URL` et `PUBLIC_BASE_URL` (voir le tableau plus
   haut). Sans ces variables, l’ingestion et le panneau de contrôle répondent 503.
6. Cliquez sur Deploy. Les pushes futurs sur la branche choisie peuvent déclencher le
   redéploiement automatique.

Le conteneur démarre `service.main:app` : le microservice et la page publique de
signature sont servis par le même processus. Les dossiers clients vivent dans le volume
`submissions` monté sur `/data`, hors de l’arborescence servie en statique.

Dokploy recommande sa configuration de domaine native plutôt que des labels Traefik
écrits à la main. Pour une démo, n’exposez dans `input/` et `output/` que des
échantillons autorisés ou anonymisés.
