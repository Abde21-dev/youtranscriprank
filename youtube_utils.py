from urllib.parse import urlparse, parse_qs
import os

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)
from youtube_transcript_api._errors import RequestBlocked


PROXY_URL = os.getenv("PROXY_URL")


def extraire_video_id(url: str) -> str:
    parsed = urlparse(url)

    hostname = (parsed.hostname or "").lower()
    path = parsed.path or ""
    query = parsed.query or ""

    # Format court : https://youtu.be/VIDEO_ID
    if "youtu.be" in hostname:
        return path.lstrip("/")

    # Formats classiques Youtube
    if "youtube.com" in hostname:
        # https://www.youtube.com/watch?v=VIDEO_ID
        if path == "/watch":
            params = parse_qs(query)
            return params.get("v", [None])[0]

        # https://www.youtube.com/embed/VIDEO_ID
        if path.startswith("/embed/"):
            return path.split("/")[2]

        # https://www.youtube.com/shorts/VIDEO_ID
        if path.startswith("/shorts/"):
            return path.split("/")[2]

    raise ValueError("URL YouTube non reconnue.")


def recuperer_transcription(video_id: str, langues=None) -> str:
    """
    Récupère la transcription YouTube en utilisant éventuellement un proxy (Oxylabs).
    """
    if langues is None:
        langues = ["fr", "en"]

    # Prépare le dict de proxies si PROXY_URL est défini
    proxies = None
    if PROXY_URL:
        proxies = {
            "http": PROXY_URL,
            "https": PROXY_URL,
        }

    try:
        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(
            video_id,
            languages=langues,
            proxies=proxies,
        )
    except RequestBlocked as e:
        raise RuntimeError(
            "YouTube bloque les requêtes du serveur (même via proxy). "
            "Réessaie plus tard ou avec une autre vidéo."
        ) from e


    # On obtient une liste de dicts équivalente à l'ancien get_transcript
    segments = fetched.to_raw_data()
    lignes = [s["text"] for s in segments if s.get("text")]
    return "\n".join(lignes)

