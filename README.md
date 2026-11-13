YouTranscriptRank 
> Application web qui transforme des vidéos YouTube en articles de blog optimisés, à partir de leur transcription et de l'API OpenAI.
![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?style=flat&logo=flask&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI_API-412991?style=flat&logo=openai&logoColor=white)
![Airtable](https://img.shields.io/badge/Airtable-18BFFF?style=flat&logo=airtable&logoColor=white)
![Stripe](https://img.shields.io/badge/Stripe-635BFF?style=flat&logo=stripe&logoColor=white)
<!-- 📸 Ajoute une capture d'écran : ![Aperçu](static/screenshot.png) -->
Démo : lien vers l'application <!-- à compléter ou supprimer -->
---
Fonctionnalités
Récupération de transcription : l'utilisateur colle l'URL d'une vidéo YouTube, l'application en extrait la transcription (avec prise en charge d'un proxy pour fiabiliser les requêtes).
Génération d'article : la transcription est envoyée à l'API OpenAI, qui rédige un article de blog structuré.
Stockage des articles dans une base Airtable.
Paiement via Stripe, avec traitement des webhooks et protection contre le double traitement d'un même événement.
Déployée en production (serveur Gunicorn, timeout ajusté pour les générations longues).
Architecture
```
├── app.py                  # Application Flask : routes, appels OpenAI, webhooks Stripe
├── youtube_utils.py        # Récupération des transcriptions YouTube (support proxy)
├── blog_utils.py           # Découpage du texte et génération de l'article
├── airtable_articles.py    # Lecture / écriture des articles dans Airtable
├── config_airtable.py      # Configuration Airtable (lue depuis les variables d'environnement)
├── templates/              # Vues HTML (Jinja2)
├── static/                 # CSS, JS, images
├── requirements.txt        # Dépendances Python
├── runtime.txt             # Version de Python
└── Procfile                # Commande de démarrage en production (Gunicorn)
```
```
URL YouTube → Transcription → Découpage (5 000 caractères max) → OpenAI → Article → Airtable
```
Installation en local
```bash
git clone https://github.com/Abde21-dev/youtranscriprank.git
cd youtranscriprank
python -m venv venv
source venv/bin/activate        # Windows : venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # puis renseigner les variables
flask --app app run --debug
```
L'application est disponible sur `http://localhost:5000`.
Variables d'environnement
Variable	Description
`OPENAI_API_KEY`	Clé de l'API OpenAI
`AIRTABLE_API_KEY`	Token d'accès Airtable
`AIRTABLE_BASE_ID`	ID de la base Airtable
`STRIPE_SECRET_KEY`	Clé secrète Stripe
`STRIPE_WEBHOOK_SECRET`	Secret de signature des webhooks Stripe
`PROXY_URL`	URL du proxy pour les requêtes YouTube (optionnel)
<!-- ⚠️ Adapte les noms aux variables réellement utilisées dans ton code -->
Sécurité
Aucune clé d'API dans le code : tous les secrets passent par des variables d'environnement (`.env` exclu via `.gitignore`).
Vérification de la signature des webhooks Stripe avant tout traitement.
Idempotence des webhooks : chaque événement Stripe n'est traité qu'une seule fois.
Validation de l'URL YouTube saisie par l'utilisateur avant tout appel externe.
Difficultés rencontrées
Timeouts en production lors des générations longues → timeout Gunicorn porté à 180 s et texte envoyé à l'API limité à 5 000 caractères.
Blocage des requêtes YouTube depuis le serveur → ajout d'un proxy.
Améliorations prévues
[ ] Tests unitaires (pytest)
[ ] Conteneurisation avec Docker
[ ] File d'attente pour les générations longues (traitement asynchrone)
---
👤 Abdramane · GitHub · LinkedIn
