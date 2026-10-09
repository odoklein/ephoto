# Certif ID – Contrôle photos ANTS & Signature (plugin WordPress)

Version 2.1.0. Écran de contrôle WooCommerce branché sur le service Python de contrôle photo/signature.

## Installation

1. Générer l'archive : `py wordpress-plugin/build_zip.py` (produit `wordpress-plugin/certif-ephoto-control.zip`).
2. WordPress > Extensions > Ajouter > Téléverser, puis activer (WooCommerce requis, PHP 7.4+).
3. Menu **Contrôle photos > Réglages** : renseigner l'adresse du service (https obligatoire ; http accepté seulement pour `localhost` / `127.0.0.1`) et les clés, puis cliquer sur **Tester la connexion**.

Les valeurs peuvent aussi être définies dans `wp-config.php` ; elles sont alors affichées grisées et ne sont jamais enregistrées en base :

```php
define( 'CERTIF_EPHOTO_SERVICE_URL', 'https://photos.exemple.fr' );
define( 'CERTIF_EPHOTO_API_KEY', '...' );        // INGEST_API_KEY du service
define( 'CERTIF_EPHOTO_REVIEW_API_KEY', '...' ); // REVIEW_API_KEY du service
```

## Les deux clés

| Réglage | Variable du service | Utilisée pour |
|---|---|---|
| Clé d'ingestion | `INGEST_API_KEY` | `POST /api/v1/ingest` (envoi des dossiers, y compris en tâche de fond) |
| Clé de contrôle | `REVIEW_API_KEY` | lecture des dossiers, images, recadrage, rotation, acceptation / refus |

- Si la clé de contrôle est vide, la clé d'ingestion est envoyée à la place. Le service refusant la clé d'ingestion sur les routes de contrôle, ce repli ne marche que si `REVIEW_API_KEY` vaut la même chose côté service : renseignez plutôt les deux clés.
- Les clés partent uniquement dans l'en-tête `X-API-Key`, jamais dans une URL ni vers le navigateur : les images passent par un proxy authentifié (`admin-ajax.php?action=certif_ephoto_image`).
- Chaque appel de contrôle envoie `X-Reviewer-User` = identifiant WordPress du contrôleur (journal d'audit du service).
- Les champs secrets ne réaffichent jamais leur valeur ; laisser le champ vide conserve la clé enregistrée.

## Une seule voie d'entrée

Les dossiers doivent arriver au service par **un seul** chemin :

- soit l'**envoi automatique** du plugin (case « Envoi automatique », activée par défaut) : à la commande payée, l'envoi est planifié en arrière-plan (Action Scheduler) ; en cas d'échec une note est ajoutée à la commande ;
- soit le **scénario Make A/A2** qui appelle `/api/v1/ingest` : dans ce cas, décocher l'envoi automatique.

Le bouton **Lancer l'IA** reste disponible : si le service a déjà le dossier (mêmes fichiers), il renvoie le dossier existant (`duplicate: true`), qui est simplement rattaché à la commande. Un dossier en cours, prêt à contrôler ou accepté n'est jamais renvoyé ; « Relancer l'IA » n'existe que pour un dossier refusé, en erreur ou purgé.

La transmission vers Make / ePhoto après acceptation est faite par le service (`MAKE_WEBHOOK_URL`), pas par WordPress.

## Droits

Toutes les actions (écran, AJAX, images) exigent la capacité `manage_woocommerce` (ou `manage_options` sans WooCommerce), modifiable par le filtre :

```php
add_filter( 'certif_ephoto_capability', function () { return 'edit_shop_orders'; } );
```

Les réglages exigent `manage_options`. Le navigateur n'envoie qu'un numéro de commande : l'identifiant du dossier est toujours relu dans la commande côté serveur.

## Fichiers acceptés

Seules les images (jpg, jpeg, png, webp, bmp, tif, tiff) hébergées sur ce site, dans le dossier `uploads`, sont envoyées au service. Les clés meta « Photo » / « Signature » des réglages sont prioritaires sur la détection automatique (TM Extra Product Options, meta d'article, meta de commande) et peuvent contenir un ID de pièce jointe image.

## Vie privée (RGPD)

- **Outils > Exporter les données personnelles** : exporte, pour chaque commande de l'e-mail de facturation, l'identifiant de dossier, le statut, les URL de la photo et de la signature envoyées, les dates et le motif.
- **Outils > Effacer les données personnelles** : supprime ces métadonnées `_certif_ephoto_*` des commandes. Les fichiers détenus par le service sont purgés par celui-ci 30 jours après la décision.
- Un texte est proposé pour la page de politique de confidentialité.

## Désinstallation

La suppression du plugin efface ses options, verrous et métadonnées `_certif_ephoto_*` de commande (tables `postmeta` et `wc_orders_meta`), sur chaque site d'un réseau multisite.
