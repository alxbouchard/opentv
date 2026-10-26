"""Aides partagées des tests Pi 4 : chemins du dépôt, import du paquet opentv_pi, catalogue de démonstration."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PI4 = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(PI4))
NANOPI = os.path.join(ROOT, "scripts", "nanopi")
if PI4 not in sys.path:
    sys.path.insert(0, PI4)

SECRET_PSK = "Tr0p-S3cret pass\\word;$(reboot)"


def catalogue_items():
    """Catalogue accepté minimal (format de catalogue.json du pont), avec URL piégées dans les textes."""
    base = [
        (101, "Le Grand Voyage", 2025, "Aventure, Drame", 7.9, 12000),
        (102, "Nuit Noire", 2019, "Thriller", 7.1, 3400),
        (103, "Rire Encore", 2021, "Comédie", 6.4, 900),
        (104, "Mission // Impossible", 2018, "Action", 7.7, 450000),
        (105, "Voir https://evil.example/x ce film", 2024, "Drame", None, None),
        (106, "Les Étoiles", 2026, "Science-fiction, Aventure", 8.2, 22000),
    ]
    out = []
    for sid, title, year, genre, rating, votes in base:
        x = {"detail_path": "b/fakrov/%d" % sid, "site_id": str(sid), "title": title, "year": year, "genre": genre,
             "description": "Résumé de %s. Plus d'infos sur www.spam.example/page" % title,
             "poster": "https://img.example/%d.jpg" % sid}
        if rating is not None:
            x.update(imdb_rating=rating, imdb_votes=votes)
        out.append(x)
    return out


def write_catalogue(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": "2026-10-26T00:00:00Z", "items": catalogue_items()}, f)
    return path
