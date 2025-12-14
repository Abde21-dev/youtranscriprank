from urllib.parse import urlparse, parse_qs
import os

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)
from youtube_transcript_api._errors import RequestBlocked
from youtube_transcript_api.proxies import GenericProxyConfig


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


def _build_api_with_proxy() -> YouTubeTranscriptApi:
    """
    Construit une instance de YouTubeTranscriptApi avec proxy si PROXY_URL est défini.
    """
    if not PROXY_URL:
        return YouTubeTranscriptApi()

    # même URL pour http et https, Oxylabs accepte ça
    proxy_config = GenericProxyConfig(
        http_url=PROXY_URL,
        https_url=PROXY_URL,
    )

    return YouTubeTranscriptApi(proxy_config=proxy_config)


def recuperer_transcription(video_id: str, langues=None) -> str:
    """
    Récupère la transcription YouTube en utilisant éventuellement un proxy (Oxylabs).
    """
    if langues is None:
        langues = ["fr", "en"]

    ytt_api = _build_api_with_proxy()

    try:
        fetched = ytt_api.fetch(
            video_id,
            languages=langues,
        )
    except RequestBlocked as e:
        # blocage IP (même via proxy)
        raise RuntimeError(
            "YouTube bloque les requêtes du serveur (même via proxy). "
            "Réessaie plus tard ou avec une autre vidéo."
        ) from e

    segments = fetched.to_raw_data()
    lignes = [s["text"] for s in segments if s.get("text")]
    return "\n".join(lignes)
