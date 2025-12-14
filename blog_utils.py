# blog_utils.py
from typing import Optional, Dict, Any
from openai import OpenAI
import json
import os

client = OpenAI()

print("OPENAI_API_KEY present:", bool(os.getenv("OPENAI_API_KEY")))

def generer_article_et_seo(
    source_text: str,
    titre_souhaite: Optional[str] = None,
    ton: str = "pédagogique et accessible",
    public_cible: str = "débutants intéressés par le sujet",
    langue: str = "français",
) -> Dict[str, Any]:

    instructions = f"""
Tu es un rédacteur web expert SEO et un content strategist.

Langue : {langue}
Ton : {ton}
Public cible : {public_cible}

Ta mission :
1. Transformer le texte source en article de blog structuré.
2. Déterminer un mot-clé principal SEO pertinent.
3. Créer un titre SEO optimisé (différent du H1 de l'article).
4. Rédiger une meta description (max 160 caractères).
5. Proposer un prompt pour une image d’illustration sans aucun texte dans l’image.

Contraintes de contenu pour l'article :
- Article en HTML uniquement (sans balises <html>, <head>, <body>).
- Utilise des balises : <h1>, <h2>, <h3>, <p>, <ul>, <ol>, <li>, <strong>, <em>, <blockquote>.
- Le H1 doit être naturel et adapté à l’article.
- Le SEO title doit être différent du H1, plus orienté “résultat Google”.
- La meta description doit faire max 160 caractères (compte très strict).
- Le mot-clé principal doit être une expression naturelle, pas une phrase entière.

Contraintes pour l'image :
- Décris une scène visuelle qui illustre le sujet de l’article.
- Interdit : texte, typographie, mots, logo, chiffres dans l’image.
- Style : illustration moderne, propre, adaptée à un blog professionnel.

Format de sortie :
Retourne UNIQUEMENT un objet JSON valide, sans texte autour, de la forme :

{{
  "html": "<h1>...</h1> ...",
  "keyword": "mot clé principal",
  "seo_title": "Titre SEO",
  "meta_description": "Meta description (max 160 caractères)",
  "image_prompt": "Description détaillée de l'image SANS texte"
}}
""".strip()

    if titre_souhaite:
        instructions += f'\n\nTitre suggéré à intégrer ou adapter : "{titre_souhaite}".'

    prompt_complet = f"{instructions}\n\nTexte source à transformer :\n\n{source_text}"

    response = client.responses.create(
        model="gpt-5.1",
        input=prompt_complet,
    )

    raw = response.output[0].content[0].text

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Impossible de parser la réponse JSON du modèle : {e}\nRéponse brute : {raw}") from e

    # Quelques sécurités basiques
    for key in ["html", "keyword", "seo_title", "meta_description", "image_prompt"]:
        data.setdefault(key, "")

    # On tronque la meta description si jamais le modèle dépasse un peu
    data["meta_description"] = data["meta_description"][:160]

    return data


def generer_image_article(image_prompt: str) -> Optional[str]:
    
    if not image_prompt:
        print("[IMAGE] Pas de prompt image fourni, aucune image générée.")
        return None

    prompt_final = (
        image_prompt.strip()
        + " ; aucun texte, aucun mot, aucune typographie dans l'image."
    )

    print("[IMAGE] Prompt final envoyé au modèle :")
    print(prompt_final)

    try:
        img_resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt_final,
            n=1,
            size="1024x1024",
        )

        print("[IMAGE] Réponse brute du modèle d'image :")
        print(img_resp)

        first = img_resp.data[0]

        # 1️⃣On essaie d'abord l'URL classique
        url = getattr(first, "url", None)

        # 2️⃣Si pas d'URL mais base64 disponible, on crée une data URL
        if not url and getattr(first, "b64_json", None):
            b64_data = first.b64_json
            url = f"data:image/png;base64,{b64_data}"

        print("[IMAGE] URL utilisée pour l'image :", url)
        return url

    except Exception as e:
        print("[IMAGE] Erreur génération image :", e)
        return None
