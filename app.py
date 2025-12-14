from flask import Flask, request, render_template, redirect, url_for, session, jsonify, abort, current_app, json
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from blog_utils import generer_article_et_seo, generer_image_article
from youtube_utils import extraire_video_id, recuperer_transcription
from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
from config_airtable import get_users_table
from airtable_articles import save_article_to_airtable, get_articles_table as get_articles_table_helper

import time
import threading
import stripe
import logging
import random
import traceback

from dotenv import load_dotenv
import os
load_dotenv()


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
stripe.api_key = os.getenv("STRIPE_API_KEY")

USER_CACHE_TTL_SECONDS = 30
# Locks in-memory pour éviter les requêtes concurrentes par user
_USER_LOCKS = {}                    # user_id -> threading.Lock()
_USER_LOCKS_MUTEX = threading.Lock()  # protège l'accès au dict _USER_LOCKS

STRIPE_PRICE_BY_PLAN = {
    "medium": os.getenv("STRIPE_PRICE_MEDIUM"),
    "premium": os.getenv("STRIPE_PRICE_PREMIUM"),
}

PRICE_TO_PLAN = {
    os.getenv("STRIPE_PRICE_MEDIUM"): "medium",
    os.getenv("STRIPE_PRICE_PREMIUM"): "premium",
    # ajoute d'autres price IDs si besoin
}

PLANS = {
    "free": {
        "price": 0,
        "credits": 5
    },
    "medium": {
        "price": 9.97,
        "credits": 20
    },
    "premium": {
        "price": 19.97,
        "credits": 50
    }
}

PLANS_AUTORISES = ["free", "medium", "premium"]

PLAN_TO_AIRTABLE_LABEL = {
    "free": "free",
    "medium": "medium",
    "premium": "premium",
}

REFRESH_ENDPOINTS = {
    "transcription",   # page de génération / affichage principale
    "blogify",         # route qui génère l'article (consommation crédit)
    "mise_a_niveau",   # si tu as nommé l'endpoint ainsi
    "upgrade",         # si tu utilises /upgrade
    "mon_compte",      # endpoint account / mon_compte
    "account",         # selon comment tu as nommé la route
    # ajoute ici d'autres endpoints critiques si tu veux
}

PROCESSED_EVENTS_FILE = "processed_events.txt"

# logger simple vers fichier
logger = logging.getLogger("upgrade")
logger.setLevel(logging.INFO)
fh = logging.FileHandler("upgrade_errors.log")
fh.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
fh.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(fh)


def _load_processed_events_file():
    try:
        with open(PROCESSED_EVENTS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

def _save_processed_event_file(event_id: str):
    with open(PROCESSED_EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(event_id + "\n")

def _is_event_processed_in_airtable(event_id: str):
    """
    Essaie d'utiliser une table 'stripe_events' dans Airtable pour stocker les event ids traités.
    Retourne True si déjà traité, False sinon.
    """
    try:
        from pyairtable import Table as PyTable
        table_events = PyTable(os.getenv("AIRTABLE_API_KEY"), os.getenv("AIRTABLE_BASE_ID"), "stripe_events")
        # rechercher si event existe (formule simple)
        formula = f"{{event_id}} = '{event_id}'"
        rec = table_events.first(formula=formula)
        return bool(rec)
    except Exception:
        return None  # None signifie "impossible de vérifier via Airtable"
    
def _mark_event_processed_in_airtable(event_id: str, event_type: str):
    try:
        from pyairtable import Table as PyTable
        table_events = PyTable(os.getenv("AIRTABLE_API_KEY"), os.getenv("AIRTABLE_BASE_ID"), "stripe_events")
        table_events.create({"event_id": event_id, "type": event_type, "created_at": int(time.time())})
        return True
    except Exception:
        return False

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", None)
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = json.loads(payload)  # fallback (moins sûr)
    except Exception as e:
        print("Webhook signature verification failed:", e)
        return abort(400)

    event_id = event.get("id")
    event_type = event.get("type")
    if not event_id:
        print("Webhook reçu sans event id")
        return "", 400

    processed_via_airtable = _is_event_processed_in_airtable(event_id)
    if processed_via_airtable is True:
        print("[Webhook] event déjà traité (airtable)", event_id)
        return "", 200

    if processed_via_airtable is None:
        processed_file_set = _load_processed_events_file()
        if event_id in processed_file_set:
            print("[Webhook] event déjà traité (fichier)", event_id)
            return "", 200

    data = event.get("data", {}).get("object", {})

    try:
        if event_type == "checkout.session.completed":
            session_obj = data
            metadata = session_obj.get("metadata", {}) or {}
            user_id = metadata.get("user_id")
            plan = metadata.get("plan")

            if not user_id or not plan:
                subscription_id = session_obj.get("subscription")
                if subscription_id:
                    try:
                        sub = stripe.Subscription.retrieve(subscription_id)
                        user_id = user_id or (sub.get("metadata") or {}).get("user_id")
                        plan = plan or (sub.get("metadata") or {}).get("plan")
                    except Exception as e:
                        print("Impossible de récupérer subscription pour fallback metadata:", e)

            if not user_id or not plan:
                print("[Webhook] checkout.session.completed sans user_id/plan, skipping")
            else:
                # identifier l'utilisateur en Airtable
                try:
                    table = get_users_table()
                    if isinstance(user_id, str) and user_id.startswith("rec"):
                        rec = table.get(user_id)
                        user_record_id = rec["id"]
                        fields = rec.get("fields", {})
                    else:
                        rec = table.first(formula=f"LOWER({{email}}) = '{user_id.lower()}'")
                        if not rec:
                            print("[Webhook] utilisateur introuvable pour checkout.session.completed:", user_id)
                            rec = None
                        else:
                            user_record_id = rec["id"]
                            fields = rec.get("fields", {})

                    if rec:
                        # detection idempotence : subscription déjà enregistrée ?
                        stripe_subscription = session_obj.get("subscription")
                        if isinstance(stripe_subscription, dict):
                            subs_id = stripe_subscription.get("id")
                        else:
                            subs_id = stripe_subscription

                        if subs_id and fields.get("stripeSubscriptionId") == subs_id:
                            print("[Webhook] checkout.session.completed : subscription déjà appliquée", subs_id)
                        else:
                            # calculer crédits à ajouter (1er paiement)
                            credits_to_add = PLANS.get(plan, {}).get("credits", 0)
                            current_credits = int(fields.get("credits", 0) or 0)
                            new_credits = current_credits + credits_to_add

                            updated = {
                                "credits": new_credits,
                                "planName": plan,
                                "status": "payant",
                            }
                            customer_id = session_obj.get("customer")
                            if customer_id:
                                updated["stripeCustomerId"] = customer_id
                            if subs_id:
                                updated["stripeSubscriptionId"] = subs_id

                            print(f"[Webhook] checkout.session.completed : user {user_record_id} +{credits_to_add} crédits (total {new_credits})")
                            table.update(user_record_id, updated)

                            # mettre à jour la session server-side si l'utilisateur est connecté
                            try:
                                sess_user = session.get("user", {})
                                if sess_user.get("id") == user_record_id:
                                    sess_user.update(
                                        {
                                            "planName": updated.get("planName"),
                                            "status": updated.get("status"),
                                            "credits": new_credits,
                                            "stripeCustomerId": updated.get("stripeCustomerId"),
                                            "stripeSubscriptionId": updated.get("stripeSubscriptionId"),
                                            "_credits_updated_at": int(time.time()),
                                        }
                                    )
                                    session["user"] = sess_user
                            except Exception:
                                pass

                except Exception as e:
                    print("Erreur traitement checkout.session.completed:", e)

        elif event_type == "invoice.payment_succeeded":
            invoice = data

            # Ne pas créditer la première facture (création d'abonnement)
            billing_reason = invoice.get("billing_reason")
            if billing_reason == "subscription_create":
                print("[Webhook] invoice.payment_succeeded (subscription_create) -> pas de crédit (déjà fait dans checkout.session.completed)")
                return "", 200

            subscription_id = invoice.get("subscription")
            customer_id = invoice.get("customer")

            if not subscription_id:
                print("[Webhook] invoice.payment_succeeded sans subscription -> skip")
            else:
                try:
                    # récupérer la subscription pour connaître le price_id
                    sub = stripe.Subscription.retrieve(subscription_id, expand=["items"])
                    items = sub.get("items", {}).get("data", [])
                    if not items:
                        print("[Webhook] Subscription sans items", subscription_id)
                    else:
                        price_id = items[0].get("price", {}).get("id")
                        plan = PRICE_TO_PLAN.get(price_id)
                        if not plan:
                            print("[Webhook] Price ID non mappé:", price_id)
                        else:
                            credits_to_add = PLANS.get(plan, {}).get("credits", 0)
                            # retrouver user via stripeCustomerId dans Airtable
                            try:
                                table = get_users_table()
                                rec = table.first(formula=f"{{stripeCustomerId}} = '{customer_id}'")
                                if not rec:
                                    print("[Webhook] Aucun utilisateur Airtable pour stripeCustomerId:", customer_id)
                                else:
                                    user_record_id = rec.get("id")
                                    fields = rec.get("fields", {})
                                    current = int(fields.get("credits", 0) or 0)
                                    new = current + credits_to_add
                                    table.update(user_record_id, {"credits": new})
                                    print(
                                        f"[Webhook] invoice.payment_succeeded : ajouté {credits_to_add} crédits à {user_record_id} (total {new})"
                                    )
                                    # update session if same user
                                    try:
                                        sess_user = session.get("user", {})
                                        if sess_user.get("id") == user_record_id:
                                            sess_user["credits"] = new
                                            sess_user["_credits_updated_at"] = int(time.time())
                                            session["user"] = sess_user
                                    except Exception:
                                        pass
                            except Exception as e:
                                print("Erreur traitement invoice.payment_succeeded (Airtable):", e)
                except Exception as e:
                    print("Erreur récupération subscription dans invoice.payment_succeeded:", e)

        elif event_type == "customer.subscription.deleted":
            sub = data
            stripe_subscription_id = sub.get("id")
            customer_id = sub.get("customer")
            try:
                table = get_users_table()
                rec = table.first(formula=f"{{stripeSubscriptionId}} = '{stripe_subscription_id}'")
                if rec:
                    user_id = rec.get("id")

                    # 1) Mise à jour Airtable : statut + plan free
                    table.update(user_id, {
                        "status": "annulé",
                        "planName": "free",
                    })
                    print(f"[Webhook] subscription.deleted : user {user_id} passé en free et marqué annulé")

                    # 2) Mise à jour de la session si l'utilisateur est connecté
                    try:
                        sess_user = session.get("user", {})
                        if sess_user.get("id") == user_id:
                            sess_user.update({
                                "status": "annulé",
                                "planName": "free",
                            })
                            session["user"] = sess_user
                    except Exception:
                        pass

            except Exception as e:
                print("Erreur traitement customer.subscription.deleted:", e)

    except Exception as e:
        
        print("Erreur traitement webhook général:", e)

    try:
        marked = _mark_event_processed_in_airtable(event_id, event_type)
        if not marked:
            _save_processed_event_file(event_id)
    except Exception as e:
        print("Impossible de marquer event comme traité (non critique):", e)

    return "", 200



@app.context_processor
def inject_user():
   
    user = get_current_user()
    if not user:
        return {"current_user": None}

    # Valeurs par défaut/initialisation dans la session si manquantes
    sess_user = session.get("user", {}) or {}
    # s'assurer que session contient l'id (devrait être le cas)
    sess_user.setdefault("id", user.get("id"))
    sess_user.setdefault("credits", sess_user.get("credits", 0))
    sess_user.setdefault("planName", sess_user.get("planName", None))
    sess_user.setdefault("status", sess_user.get("status", None))
    sess_user.setdefault("_credits_updated_at", sess_user.get("_credits_updated_at", 0))

    # décider si on rafraîchit depuis Airtable
    endpoint = (request.endpoint or "").split(".")[-1]  # support blueprint.endpoint
    now_ts = int(time.time())
    cache_age = now_ts - int(sess_user.get("_credits_updated_at", 0) or 0)

    should_refresh = False
    if endpoint in REFRESH_ENDPOINTS:
        should_refresh = True
    elif cache_age > USER_CACHE_TTL_SECONDS:
        should_refresh = True

    if should_refresh:
        try:
            table = get_users_table()
            record = table.get(sess_user["id"])
            fields = record.get("fields", {})

            # Extrait et normalise les champs attendus
            credits_val = int(fields.get("credits", 0) or 0)
            plan_val = fields.get("planName", sess_user.get("planName"))
            status_val = fields.get("status", sess_user.get("status"))

            # met à jour la session
            sess_user["credits"] = credits_val
            sess_user["planName"] = plan_val
            sess_user["status"] = status_val
            sess_user["_credits_updated_at"] = now_ts

            session["user"] = sess_user
            user = sess_user  # on retourne la version enrichie
        except Exception as e:
            # En cas d'erreur : on ne plante pas l'app, on log et on garde les données en session
            print("Warning inject_user(): impossible de rafraîchir Airtable :", e)
            
            user = sess_user
    else:
        # cache encore valide -> on utilise la session
        user = sess_user

    return {"current_user": user}

def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not get_current_user():
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper   

def get_current_user():
    
    return session.get("user")


def consume_credit_for_user(user_id: str) -> int:
    """
    Lit la valeur actuelle des crédits, vérifie >=1, la décrémente de 1,
    met à jour Airtable et renvoie le nouveau solde.
    Lève ValueError si solde insuffisant.
    """
    table = get_users_table()
    record = table.get(user_id)
    fields = record.get("fields", {})
    credits = int(fields.get("credits", 0) or 0)

    if credits <= 0:
        raise ValueError("Solde de crédits insuffisant.")

    new_credits = credits - 1
    table.update(record["id"], {"credits": new_credits})
    return new_credits
    

@app.route("/", methods=["GET", "POST"])
@app.route("/transcription", methods=["GET", "POST"])
@login_required
def transcription():
    transcript = None
    erreur = None
    video_id = None
    url = None
    article_html = None
    article_id = None
    seo_keyword = None
    seo_title = None
    meta_description = None
    image_url = None

    credits_left = None
    user = get_current_user()
    if user:
        try:
            table = get_users_table()
            record = table.get(user["id"])
            fields = record.get("fields", {})
            credits_left = int(fields.get("credits", 0) or 0)
            # Met à jour la session côté serveur pour garder l'UI synchronisée
            session_user = session.get("user", {}) or {}
            session_user["credits"] = credits_left
            session["user"] = session_user
        except Exception as e:
            # Ne bloque pas l'affichage si Airtable a un problème : on log localement et continue
            print("Erreur récupération crédits Airtable :", e)
            credits_left = session.get("user", {}).get("credits", 0)

   
    if request.method == "POST":
        url = request.form.get("url", "").strip()

        try:
            if not url:
                raise ValueError("Merci de fournir une URL YouTube.")

            video_id = extraire_video_id(url)
            if not video_id:
                raise ValueError("Impossible d'extraire l'ID de la vidéo.")

            transcript = recuperer_transcription(video_id, langues=["fr", "en"])

        except ValueError as e:
            erreur = str(e)
        except TranscriptsDisabled:
            erreur = "Cette vidéo n'a pas de transcription disponible (transcriptions désactivées)."
        except NoTranscriptFound:
            erreur = "Aucune transcription trouvée pour cette vidéo (ni en FR ni en EN)."
        except VideoUnavailable:
            erreur = "Cette vidéo est indisponible."
        except Exception as e:
            erreur = f"Erreur inattendue : {e}"

    # rendu final
    return render_template(
        "transcription.html",
        active_page="transcription",
        transcript=transcript,
        erreur=erreur,
        video_id=video_id,
        url=url,
        article_html=article_html,
        article_id=article_id,
        seo_keyword=seo_keyword,
        seo_title=seo_title,
        meta_description=meta_description,
        image_url=image_url,
        credits_left=credits_left,
    )



@app.route("/blogify", methods=["POST"])
@login_required
def blogify():
    transcript = request.form.get("source_text", "").strip()
    titre_souhaite = request.form.get("titre_souhaite", "").strip() or None
    with_image = request.form.get("with_image")  # "1" si coché, None sinon

    article_html = None
    seo_keyword = None
    seo_title = None
    meta_description = None
    image_url = None
    erreur = None
    warning = None
    article_record_id = None

    if not transcript:
        erreur = "Aucun texte à transformer. Commence par générer une transcription."
        return render_template("transcription.html", active_page="transcription", transcript=transcript, erreur=erreur)

    user = get_current_user()
    if not user:
        erreur = "Utilisateur non authentifié."
        return render_template("transcription.html", active_page="transcription", transcript=transcript, erreur=erreur)

    user_id = user.get("id")
    if not user_id:
        erreur = "Erreur interne : identifiant utilisateur manquant."
        return render_template("transcription.html", active_page="transcription", transcript=transcript, erreur=erreur)

    # Lecture du solde
    try:
        table = get_users_table()
        record = table.get(user_id)
        fields = record.get("fields", {})
        credits_current = int(fields.get("credits", 0) or 0)
    except Exception as e:
        print("Erreur récupération crédits avant blogify :", e)
        credits_current = int(session.get("user", {}).get("credits", 0) or 0)

    # Coût : 1 crédit pour l'article, +2 si image demandée
    cost_for_article = 1
    cost_for_image = 2 if with_image else 0
    total_cost = cost_for_article + cost_for_image

    if credits_current < total_cost:
        if with_image and credits_current >= 1:
            # Pas assez pour l'image, mais assez pour l'article seul
            warning = "Solde insuffisant pour générer l'image. L'article sera généré sans illustration."
            with_image = None  # on désactive l'image, on ne prendra que 1 crédit
            total_cost = 1
        else:
            erreur = "Solde insuffisant : vous n’avez plus assez de crédits."
            return render_template("transcription.html", active_page="transcription", transcript=transcript, erreur=erreur)
 
    try:
        data = generer_article_et_seo(
            transcript,
            titre_souhaite=titre_souhaite,
            ton="pédagogique et accessible",
            public_cible="grand public intéressé par le sujet",
            langue="français",
        )
        article_html = data.get("html") or data.get("article_html") or ""
        seo_keyword = data.get("keyword")
        seo_title = data.get("seo_title")
        meta_description = data.get("meta_description")
        image_prompt = data.get("image_prompt", "")
    except Exception as e:
        erreur = f"Erreur lors de la génération de l'article : {e}"
        return render_template("transcription.html", active_page="transcription", transcript=transcript, erreur=erreur)
        print("ERREUR GENERATION ARTICLE:", repr(e))
        traceback.print_exc(
    
    try:
        rec = table.get(user_id)
        current_after = int(rec.get("fields", {}).get("credits", 0) or 0)
    except Exception as e:
        print("Erreur relecture crédits avant décrémentation :", e)
        current_after = int(session.get("user", {}).get("credits", 0) or 0)

    if current_after < total_cost:
        warning = "L'article a été généré mais le solde est insuffisant au moment de la finalisation."
    else:
        try:
            new_credits = current_after - total_cost
            table.update(user_id, {"credits": new_credits})
            session_user = session.get("user", {}) or {}
            session_user["credits"] = new_credits
            session_user["_credits_updated_at"] = int(time.time())
            session["user"] = session_user
        except Exception as e:
            warning = f"L'article a été généré, mais impossible de mettre à jour les crédits : {e}"
            print("Erreur mise à jour credits après génération :", e)

    # Génération image (seulement si demandé)
    if with_image:
        try:
            image_url = generer_image_article(image_prompt)
        except Exception as e:
            print("Erreur génération image (non bloquante) :", e)
            image_url = None

    # Sauvegarde dans Airtable
    try:
        title_to_save = seo_title or (titre_souhaite or "Article généré")
        record = save_article_to_airtable(
            user_id,
            title=title_to_save,
            seo_title=seo_title,
            keyword=seo_keyword,
            meta_description=meta_description,
            html_content=article_html,
            image_url=None,
            source_video_id=None,
            source_transcript=None,
            credits_used=total_cost,
            status="draft"
        )
        article_record_id = record.get("id")
      
        session_user = session.get("user", {}) or {}
        session_user["last_article_id"] = article_record_id
        session_user["_credits_updated_at"] = int(time.time())
        session["user"] = session_user

    except Exception as e:
        print("Erreur sauvegarde article Airtable :", e)
        warning = (warning or "") + " Erreur lors de la sauvegarde de l'article."

    return render_template(
        "transcription.html",
        active_page="transcription",
        transcript=transcript,
        erreur=erreur,
        article_html=article_html,
        seo_keyword=seo_keyword,
        seo_title=seo_title,
        meta_description=meta_description,
        image_url=image_url,
        warning=warning,
        article_id=article_record_id,
    )




@app.route("/articles")
@login_required
def mes_articles():
    return redirect(url_for("mes_articles_list"))

@app.route("/mes-articles")
@login_required
def mes_articles_list():
    user = get_current_user()
    articles = []
    try:
        table = get_articles_table_helper()
        records = table.all()
        for r in records:
            fields = r.get("fields", {})
            linked = fields.get("user") or []  
            if user and linked and user.get("id") in linked:
                created_raw = r.get("createdTime")  
                created_fmt = None
                if created_raw:
                    try:
                        dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                        created_fmt = dt.strftime("%d/%m/%y")  # jj/mm/aa
                    except Exception:
                        created_fmt = created_raw  # fallback

                articles.append({
                    "id": r.get("id"),
                    "title": fields.get("title"),
                    "seo_title": fields.get("seo_title"),
                    "keyword": fields.get("keyword"),
                    "created_at": created_fmt,
                    "status": fields.get("status"),
                })
    except Exception as e:
        print("Erreur récupération articles :", e)
        articles = []

    return render_template("mes_articles.html", articles=articles, active_page="mes_articles", title="Mes articles – YouTranscripRank")


@app.route("/article/<article_id>")
@login_required
def voir_article(article_id):
    try:
        table = get_articles_table_helper()
        rec = table.get(article_id)
    except Exception as e:
        return f"Article introuvable : {e}", 404

    fields = rec.get("fields", {})
    html_content = fields.get("html_content", "")
    # Si tu veux nettoyer -> utiliser bleach (optionnel)
    return render_template("article_view.html", article=fields, html_content=html_content)


@app.route("/upgrade", methods=["GET", "POST"])
@login_required
def mise_a_niveau():
    user = get_current_user()
    table = get_users_table()
    erreur = None
    success = None

    try:
        record = table.get(user["id"])
    except Exception as e:
        return f"Erreur lors de la récupération du compte : {e}"

    fields = record.get("fields", {})
    current_plan = fields.get("planName", "free")
    current_credits = fields.get("credits", 0)

    if request.method == "POST":
        new_status = request.form.get("status")  # "free" | "medium" | "premium"
        if not new_status:
            erreur = "Aucune formule sélectionnée."
        elif new_status not in PLANS:
            erreur = "Formule sélectionnée invalide."
        else:
            # ID du record Airtable
            user_id = record["id"]

            try:
                # Met à jour planCredits, credits et status (gratuit / payant)
                updated_fields = {
                "planName": new_status,
                "status": "payant" if new_status != "free" else "gratuit"
                }
                table.update(user_id, updated_fields)

                session_user = session.get("user", {})
                session_user.update({
                "status": updated_fields["status"],
                "planName": new_status,
                # NE PAS toucher à "credits" ici
                })
                session["user"] = session_user


                # Mettre à jour variables locales pour affichage
                current_plan = new_status
                current_credits = PLANS[new_status]["credits"]
                success = "Votre formule a été mise à jour avec succès."
            except Exception as e:
                return f"Erreur lors de la mise à jour du plan : {e}"

    return render_template(
        "mise_a_niveau.html",
        title="Mise à niveau – YouTranscripRank",
        active_page="mise_a_niveau",
        status=current_plan,
        credits=current_credits,
        erreur=erreur,
        success=success,
    )

@app.route("/create-checkout-session/<plan>", methods=["POST"])
@login_required
def create_checkout_session(plan):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Utilisateur non authentifié."}), 401

    if plan not in STRIPE_PRICE_BY_PLAN or not STRIPE_PRICE_BY_PLAN[plan]:
        return jsonify({"error": "Plan invalide ou non configuré."}), 400

    price_id = STRIPE_PRICE_BY_PLAN[plan]

        # 1) Annuler l'ancien abonnement Stripe s'il existe
    try:
        table = get_users_table()
        record = table.get(user["id"])
        fields = record.get("fields", {})
        current_sub_id = fields.get("stripeSubscriptionId")
    except Exception as e:
        print("Erreur récupération user avant upgrade :", e)
        current_sub_id = None

    if current_sub_id:
        try:
            # Annulation immédiate
            stripe.Subscription.delete(current_sub_id)
            print(f"[Upgrade] Ancienne subscription {current_sub_id} annulée immédiatement.")
        except Exception as e:
            print("Erreur annulation ancienne subscription Stripe :", e)

    try:
        session_stripe = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=url_for("upgrade_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("mise_a_niveau", _external=True),
            metadata={
                "user_id": user.get("id"),
                "plan": plan
            },
        )
        # Retourner l'URL de redirection
        return jsonify({"url": session_stripe.url})
    except Exception as e:
        print("Erreur création Stripe session:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/upgrade/success")
@login_required
def upgrade_success():
    session_id = request.args.get("session_id")
    if not session_id:
        return "Session Stripe introuvable", 400

    try:
        checkout_session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["subscription", "customer"],
        )
    except Exception as e:
        print("Erreur récupération session Stripe:", e)
        return f"Erreur lors de la vérification du paiement : {e}", 500

    metadata = checkout_session.get("metadata", {}) or {}
    user_id = metadata.get("user_id")
    plan = metadata.get("plan")

    sub = checkout_session.get("subscription")
    if (not user_id or not plan) and isinstance(sub, dict):
        user_id = user_id or (sub.get("metadata") or {}).get("user_id")
        plan = plan or (sub.get("metadata") or {}).get("plan")

    if not user_id or not plan:
        return "Données utilisateur manquantes dans la session Stripe.", 400

    stripe_customer_id = checkout_session.get("customer")
    stripe_subscription_id = sub.get("id") if isinstance(sub, dict) else sub

    try:
        table = get_users_table()

        # retrouver le record Airtable
        if isinstance(user_id, str) and user_id.startswith("rec"):
            rec = table.get(user_id)
        else:
            rec = table.first(formula=f"LOWER({{email}}) = '{user_id.lower()}'")
            if not rec:
                raise RuntimeError("Utilisateur introuvable en Airtable.")

        user_record_id = rec["id"]
        fields = rec.get("fields", {})

        # compléter eventuels champs Stripe manquants, SANS toucher aux crédits
        updates = {}
        if stripe_customer_id and not fields.get("stripeCustomerId"):
            updates["stripeCustomerId"] = stripe_customer_id
        if stripe_subscription_id and not fields.get("stripeSubscriptionId"):
            updates["stripeSubscriptionId"] = stripe_subscription_id
        if updates:
            print("DEBUG Airtable update user", user_record_id, "updates=", updates)
            table.update(user_record_id, updates)
            rec = table.get(user_record_id)
            fields = rec.get("fields", {})

        # synchro session avec l'état réel (webhook)
        sess_user = session.get("user", {}) or {}
        sess_user.update(
            {
                "planName": fields.get("planName") or plan,
                "status": fields.get("status") or "payant",
                "credits": int(fields.get("credits", 0) or 0),
                "stripeCustomerId": fields.get("stripeCustomerId") or stripe_customer_id,
                "stripeSubscriptionId": fields.get("stripeSubscriptionId") or stripe_subscription_id,
                "_credits_updated_at": int(time.time()),
            }
        )
        session["user"] = sess_user

        return render_template(
            "upgrade_success.html",
            added=0,
            new_credits=sess_user["credits"],
            plan=plan,
        )

    except Exception as e:
        import traceback
        print("Échec mise à jour Airtable dans upgrade_success:", e)
        traceback.print_exc()
        user_msg = (
            "Le paiement a été confirmé, mais impossible de mettre à jour votre compte automatiquement. "
            "Nous avons enregistré l'incident et nous allons le traiter. "
            f"Code de référence : {session_id[:12]}..."
        )
        return (
            render_template(
                "upgrade_success.html",
                error=user_msg,
                added=0,
                new_credits=None,
                plan=plan,
            ),
            500,
        )


@app.route("/upgrade/cancel")
@login_required
def upgrade_cancel():
    return render_template("upgrade_cancel.html", title="Paiement annulé")

@app.route("/cancel-subscription", methods=["POST"])
@login_required
def cancel_subscription():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    user_id = user.get("id")
    try:
        table = get_users_table()
        record = table.get(user_id)
        fields = record.get("fields", {})
        stripe_subscription_id = fields.get("stripeSubscriptionId")
        if not stripe_subscription_id:
            # rien à annuler côté Stripe
            return redirect(url_for("mon_compte"))

        # Annuler à la fin de la période actuelle 
        stripe.Subscription.modify(
            stripe_subscription_id,
            cancel_at_period_end=True,
        )

        # Flag pour la popup de succès
        session_user = session.get("user", {}) or {}
        session_user["cancel_success"] = True
        session["user"] = session_user

        return redirect(url_for("mon_compte"))

    except Exception as e:
        print("Erreur annulation subscription:", e)
        return redirect(url_for("mon_compte"))

@app.route("/account", methods=["GET", "POST"])
@login_required
def mon_compte():
    user = get_current_user()
    table = get_users_table()
    erreur = None
    success = None

    # Charger le record Airtable de l'utilisateur
    try:
        record = table.get(user["id"])
    except Exception as e:
        return f"Erreur lors de la récupération du compte : {e}"

    fields = record.get("fields", {})
    email = fields.get("email", "")
    
    creation_raw = fields.get("creationDate", "")
    creation_date = creation_raw
    if creation_raw:
        try:
            dt = datetime.fromisoformat(creation_raw.replace("Z", "+00:00"))
            creation_date = dt.strftime("%d/%m/%y")  # jj/mm/aa
        except Exception:
            creation_date = creation_raw 

    # Valeurs actuelles
    current_plan = fields.get("planName", "free")
    credits_left = int(fields.get("credits", 0) or 0)
    status = fields.get("status", "")  

    if request.method == "POST":
        action = request.form.get("action")  

        if action == "update_plan":
            new_plan = request.form.get("status")  # valeur: "free"|"medium"|"premium"
            if not new_plan:
                erreur = "Aucune formule sélectionnée."
            elif new_plan not in PLANS_AUTORISES:
                erreur = "Formule sélectionnée invalide."
            else:
                # map vers l'étiquette attendue par Airtable
                airtable_label = PLAN_TO_AIRTABLE_LABEL.get(new_plan)
                if not airtable_label:
                    erreur = "Configuration interne manquante pour ce plan."
                else:
                    try:
                        user_id = record["id"]
                        updated_fields = {
                        "planName": airtable_label,
                        "status": "payant" if new_plan != "free" else "gratuit"
                        # pas de "credits" ici pour les plans payants
                        }
                        table.update(user_id, updated_fields)

                        current_plan = updated_fields["planName"]
                        status = updated_fields["status"]

                        session_user = session.get("user", {})
                        session_user.update({
                        "planName": current_plan,
                        "credits": credits_left,   
                        "status": status
                        })
                        session["user"] = session_user


                        success = "Votre formule a été mise à jour avec succès."
                    except Exception as e:
                        erreur = f"Erreur lors de la mise à jour du plan : {e}"

        # ---------- Changement de mot de passe ----------
        elif action == "update_password":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""

            # validations
            if not current_password or not new_password or not confirm_password:
                erreur = "Merci de remplir tous les champs de mot de passe."
            elif new_password != confirm_password:
                erreur = "La confirmation du nouveau mot de passe ne correspond pas."
            elif len(new_password) < 6:
                erreur = "Le nouveau mot de passe doit contenir au moins 6 caractères."
            else:
                stored_hash = fields.get("password")
                if not stored_hash or not check_password_hash(stored_hash, current_password):
                    erreur = "L'ancien mot de passe est incorrect."
                else:
                    new_hash = generate_password_hash(new_password)
                    try:
                        table.update(record["id"], {"password": new_hash})
                        # message de succès
                        success = "Votre mot de passe a été mis à jour avec succès."
                    except Exception as e:
                        erreur = f"Erreur lors de la mise à jour du mot de passe : {e}"

        else:
            
            erreur = "Action inconnue."

    # Lire le flag de succès de désabonnement
    sess_user = session.get("user", {}) or {}
    if sess_user.pop("cancel_success", None):
        success = "Votre désabonnement a bien été pris en compte."
        session["user"] = sess_user        

    return render_template(
        "mon_compte.html",
        title="Mon compte – YouTranscripRank",
        active_page="mon_compte",
        email=email,
        status=current_plan,
        creation_date=creation_date,
        credits_left=credits_left,
        erreur=erreur,
        success=success,
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    table = get_users_table()
    erreur = None
    email_value = ""

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        email_value = email

        if not email or not password:
            erreur = "Merci de renseigner un e-mail et un mot de passe."
        elif "@" not in email:
            erreur = "Merci de saisir une adresse e-mail valide."
        elif len(password) < 6:
            erreur = "Le mot de passe doit contenir au moins 6 caractères."
        else:
            try:
                formula = f"LOWER({{email}}) = '{email}'"
                existing = table.first(formula=formula)
            except Exception as e:
                return f"Erreur lors de la vérification de l'utilisateur : {e}"

            if existing:
                erreur = "Un compte existe déjà avec cet e-mail."
            else:
                password_hash = generate_password_hash(password)

                # Générer un code de confirmation à 6 chiffres
                code = str(random.randint(100000, 999999))

                try:
                    new_record = table.create({
                        "email": email,
                        "password": password_hash,
                        "status": "gratuit",
                        "planName": "free",
                        "credits": 5,
                        "isConfirmed": False,
                        "confirmationCode": code,
                    })
                except Exception as e:
                    return f"Erreur Airtable : {e}"

                # TODO : envoyer `code` par e-mail à l'utilisateur

                # Stocker juste l'id en session pour la confirmation
                session["pending_user_id"] = new_record["id"]
                session["pending_email"] = email

                return redirect(url_for("confirm_signup"))

    return render_template(
        "signup.html",
        title="Créer un compte – YouTranscripRank",
        active_page="signup",
        erreur=erreur,
        email_value=email_value,
    )

@app.route("/confirm", methods=["GET", "POST"])
def confirm_signup():
    table = get_users_table()
    erreur = None
    success = None

    # On récupère email éventuel pour préremplir
    email_value = session.get("pending_email", "")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        code = (request.form.get("code") or "").strip()

        if not email or not code:
            erreur = "Merci de renseigner l'e-mail et le code de confirmation."
        else:
            try:
                formula = f"LOWER({{email}}) = '{email}'"
                record = table.first(formula=formula)
            except Exception as e:
                return f"Erreur lors de la recherche de l'utilisateur : {e}"

            if not record:
                erreur = "Aucun compte trouvé avec cet e-mail."
            else:
                fields = record.get("fields", {})
                if fields.get("isConfirmed"):
                    erreur = "Ce compte est déjà confirmé. Vous pouvez vous connecter."
                elif fields.get("confirmationCode") != code:
                    erreur = "Code de confirmation incorrect."
                else:
                    # Valider le compte
                    try:
                        table.update(record["id"], {
                            "isConfirmed": True,
                            "confirmationCode": "",
                        })
                    except Exception as e:
                        return f"Erreur lors de la confirmation du compte : {e}"

                    # Connexion après confirmation
                    session["user"] = {
                        "id": record["id"],
                        "email": fields.get("email"),
                        "status": fields.get("status", "gratuit"),
                        "planName": fields.get("planName", "free"),
                        "credits": int(fields.get("credits", 0) or 0),
                    }

                    # Nettoyage de la session temporaire
                    session.pop("pending_user_id", None)
                    session.pop("pending_email", None)

                    return redirect(url_for("transcription"))

    return render_template(
        "confirm_signup.html",
        title="Confirmer votre compte",
        erreur=erreur,
        success=success,
        email_value=email_value,
    )



@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    table = get_users_table()
    erreur = None
    email_value = ""

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        email_value = email

        if not email or not password:
            erreur = "Merci de renseigner un e-mail et un mot de passe."
        else:
            try:
                formula = f"LOWER({{email}}) = '{email}'"
                record = table.first(formula=formula)
            except Exception as e:
                return f"Erreur lors de la recherche de l'utilisateur : {e}"

            if not record:
                erreur = "Aucun compte trouvé avec cet e-mail."
            else:
                fields = record.get("fields", {})
                password_hash = fields.get("password")

                if not password_hash:
                    erreur = "Ce compte n'a pas de mot de passe défini."
                elif not check_password_hash(password_hash, password):
                    erreur = "Mot de passe incorrect."
                else:
                    # Auth OK → on stocke en session
                    session["user"] = {
                        "id": record.get("id"),
                        "email": fields.get("email"),
                        "status": fields.get("status", None),
                    }
                    # Redirection vers la page principale
                    return redirect(url_for("transcription"))

    return render_template(
        "login.html",
        title="Connexion – YouTranscripRank",
        active_page= "login", #"mon_compte",
        erreur=erreur,
        email_value=email_value,
    )


@app.route("/debug/users")
def debug_users():
    table = get_users_table()  # si l'import est bon, plus de NameError ici
    try:
        records = table.all(max_records=5)
    except Exception as e:
        return f"Erreur Airtable : {e}"

    html = "<h1>Users – Airtable</h1><ul>"
    for rec in records:
        fields = rec.get("fields", {})
        email = fields.get("e-mail", "—")
        status = fields.get("status", "—")
        html += f"<li>{email} – {status}</li>"
    html += "</ul>"
    return html
 


if __name__ == "__main__":
    app.run(debug=True)
