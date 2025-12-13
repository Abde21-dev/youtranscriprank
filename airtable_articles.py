# airtable_articles.py
from pyairtable import Table
import os
import time

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
ARTICLES_TABLE = os.getenv("AIRTABLE_ARTICLES_TABLE", "articles")  # default "articles"

def get_articles_table():
    return Table(AIRTABLE_API_KEY, AIRTABLE_BASE_ID, ARTICLES_TABLE)

def save_article_to_airtable(user_record_id: str, *,
                             title: str,
                             seo_title: str = None,
                             keyword: str = None,
                             meta_description: str = None,
                             html_content: str = None,
                             image_url: str = None,
                             source_video_id: str = None,
                             source_transcript: str = None,
                             credits_used: int = 1,
                             status: str = "draft"):
  
    table = get_articles_table()

    fields = {
        "title": title or "Sans titre",
        "seo_title": seo_title,
        "keyword": keyword,
        "meta_description": (meta_description or "")[:160],
        "html_content": html_content or "",
        "image_url": image_url,
        "source_video_id": source_video_id,
        "source_transcript": source_transcript,
        "credits_used": credits_used,
        "status": status,
    }

    if user_record_id:
        fields["user"] = [user_record_id]

    fields = {k: v for k, v in fields.items() if v is not None}

    record = table.create(fields)
    return record
