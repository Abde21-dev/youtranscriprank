# config_airtable.py
import os
from pyairtable import Table

# 🔐 Chargement des variables d'environnement
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_USERS_TABLE = os.getenv("AIRTABLE_USERS_TABLE", "users")

def get_users_table() -> Table:
    """
    Retourne un objet Table connecté à la table 'users'.
    """
    if not AIRTABLE_API_KEY:
        raise ValueError("❌ Variable d'environnement AIRTABLE_API_KEY manquante")

    if not AIRTABLE_BASE_ID:
        raise ValueError("❌ Variable d'environnement AIRTABLE_BASE_ID manquante")

    return Table(AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_USERS_TABLE)



