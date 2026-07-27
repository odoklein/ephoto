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

BioGaze is not available from PyPI. If the client supplies its vendor wheel/private
index, install it separately; `--require-biogaze` then makes validation stop unless
the module is available. Its public Python processing API is not stable enough to
safely invoke by name, so the deterministic OpenCV path performs the pixel work.

The validation checks are technical local heuristics. They cannot certify ANTS or
certif-idphoto.fr acceptance; test an exported file in the actual intake system
before making that claim. Adjust the service’s actual file limit with `--max-bytes`.

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
5. Cliquez sur Deploy. Les pushes futurs sur la branche choisie peuvent déclencher le
   redéploiement automatique.

Dokploy recommande sa configuration de domaine native plutôt que des labels Traefik
écrits à la main. Pour une démo, n’exposez dans `input/` et `output/` que des
échantillons autorisés ou anonymisés.
