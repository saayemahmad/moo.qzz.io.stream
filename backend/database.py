import os
import json
import firebase_admin
from firebase_admin import credentials
from firebase_admin import db

DATABASE_URL = 'https://dev-ca098-default-rtdb.firebaseio.com/'

if not firebase_admin._apps:
    _sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if _sa_json:
        # Production: load from environment variable (Render, etc.)
        _sa_dict = json.loads(_sa_json)
        cred = credentials.Certificate(_sa_dict)
    else:
        # Local dev: fall back to serviceAccountKey.json file
        _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _credentials_path = os.path.join(_BASE_DIR, "serviceAccountKey.json")
        cred = credentials.Certificate(_credentials_path)

    firebase_admin.initialize_app(cred, {'databaseURL': DATABASE_URL})

def get_db():
    return db
