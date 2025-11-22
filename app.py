from flask import Flask, request, render_template

from youtube_utils import (
    extraire_video_id,
    recuperer_transcription,
)
from youtube_transcript_api import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
def index():
    transcript = None
    erreur = None
    video_id = None
    url = None

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

    return render_template(
        "index.html",
        transcript=transcript,
        erreur=erreur,
        video_id=video_id,
        url=url,
    )


if __name__ == "__main__":
    app.run(debug=True)
