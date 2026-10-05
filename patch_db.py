from backend.database import get_db
from pathlib import Path
db = get_db()
uploads_ref = db.reference('uploads').get() or {}
for uid, s in uploads_ref.items():
    if s.get('status') in ['ready', 'packaging']:
        if not (Path('media') / uid).is_dir():
            if not s.get('b2_direct'):
                print(f"Fixing {uid}")
                db.reference(f'uploads/{uid}').update({'b2_direct': True})
