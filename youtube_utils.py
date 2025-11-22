from urllib.parse import urlparse, parse_qs

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)


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
    Récupère la transcription sous forme de texte brut
    pour la version 1.2.3 de youtube-transcript-api.
    """
    if langues is None:
        langues = ["fr", "en"]

    # Création de l'instance de l’API
    ytt_api = YouTubeTranscriptApi()

    # Récupération de la transcription (FetchedTranscript)
    fetched = ytt_api.fetch(video_id, languages=langues)

    # On obtient une liste de dicts équivalente à l'ancien get_transcript
    segments = fetched.to_raw_data()

    lignes = [s["text"] for s in segments if s.get("text")]
    return "\n".join(lignes)
