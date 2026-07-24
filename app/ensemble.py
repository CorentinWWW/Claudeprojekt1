"""Ensemble-Modell: eine von Claude UNABHAENGIGE, klassische Zweitmeinung.

Bag-of-Words Bernoulli-Naive-Bayes (dieselbe Grundidee wie ein klassischer
Spam-Filter), das direkt aus der EIGENEN bisherigen Erfolgsbilanz lernt
(alert_outcomes JOIN statements) - ob Meldungen mit AEHNLICHEM Wortschatz
frueher eher zu einem Treffer oder einem Fehlschlag gefuehrt haben. Kein
externes ML-Framework, keine separate Trainingsinfrastruktur: die Trainingsdaten
sind exakt die, die dieses System durch ENABLE_PRICE_TRACKING ohnehin sammelt,
und das Training selbst ist reine Wortzaehlung (Millisekunden fuer die hier
realistische Datenmenge).

Bewusst simpel und interpretierbar statt eines schwergewichtigen ML-Stacks
(numpy/scikit-learn wuerden die Startzeit + Paket-Fragilitaet des ohnehin schon
48x/Tag frisch installierten GitHub-Actions-Laufs unnoetig erhoehen, siehe
requirements.txt)."""
import logging
import math
import re
import time
from typing import Optional

from app.db import get_conn

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-zA-ZäöüÄÖÜß]{3,}")
# Kleine, sprachunabhaengig kuratierte Stoppwortliste - haeufige Fuellwoerter tragen
# kein Klassifikations-Signal und wuerden nur das Vokabular unnoetig aufblaehen.
_STOPWORDS = {
    "the", "and", "for", "are", "with", "was", "were", "has", "have", "had",
    "its", "his", "her", "that", "this", "from", "will", "would", "could",
    "der", "die", "das", "und", "fuer", "für", "mit", "auf", "ist", "sich",
    "eine", "einen", "einer", "nach", "auch", "wird", "wurde", "werden",
}

# In-Memory-Cache (analog zu app.prices): Training ist billig, aber unnoetig bei
# jedem einzelnen Statement innerhalb desselben Poll-Zyklus/Prozesses.
_model_cache: dict = {"trained_at": 0.0, "model": None}


def _tokenize(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    return {w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOPWORDS}


def _train_from_db(min_samples: int) -> Optional[dict]:
    """Liest alle ausgewerteten Ergebnisse (correct IS NOT NULL) mitsamt Statement-Text
    und baut ein Bernoulli-Naive-Bayes-Modell (Wort GESEHEN oder NICHT, keine
    Frequenzen). None, wenn zu wenige Datenpunkte vorliegen ODER nur EINE der beiden
    Klassen (Treffer/Fehlschlag) existiert - dann waere jede Vorhersage trivial 100%
    oder 0% und liefert keinen echten Zweitmeinungs-Wert."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.text AS text, o.correct AS correct
            FROM alert_outcomes o
            JOIN statements s ON o.statement_id = s.id
            WHERE o.correct IS NOT NULL
            """
        ).fetchall()
    if len(rows) < min_samples:
        return None

    hit_counts: dict[str, int] = {}
    miss_counts: dict[str, int] = {}
    hit_docs = 0
    miss_docs = 0
    for row in rows:
        words = _tokenize(row["text"])
        if not words:
            continue
        is_hit = bool(row["correct"])
        target = hit_counts if is_hit else miss_counts
        for w in words:
            target[w] = target.get(w, 0) + 1
        if is_hit:
            hit_docs += 1
        else:
            miss_docs += 1

    if hit_docs == 0 or miss_docs == 0:
        return None

    vocab_size = len(set(hit_counts) | set(miss_counts))
    return {
        "hit_counts": hit_counts,
        "miss_counts": miss_counts,
        "hit_docs": hit_docs,
        "miss_docs": miss_docs,
        "vocab_size": vocab_size,
        "n": hit_docs + miss_docs,
    }


def get_model(min_samples: int, retrain_seconds: float = 900.0) -> Optional[dict]:
    """Liefert das aktuell trainierte Modell, trainiert bei Bedarf neu (Cache mit TTL).
    None, solange nicht genug ausgewertete Ergebnisse BEIDER Klassen vorliegen - der
    Aufrufer behandelt das als 'Ensemble-Modell noch nicht einsatzbereit' (kein Effekt,
    kein Fehler)."""
    now = time.time()
    cached = _model_cache["model"]
    if cached is not None and (now - _model_cache["trained_at"]) < retrain_seconds:
        return cached
    try:
        model = _train_from_db(min_samples)
    except Exception:
        logger.warning("[ensemble] Training fehlgeschlagen (best-effort).", exc_info=True)
        model = None
    _model_cache["model"] = model
    _model_cache["trained_at"] = now
    return model


def predict_hit_probability(model: dict, text: Optional[str]) -> Optional[float]:
    """Bernoulli-Naive-Bayes-Schaetzung (Laplace-geglaettet): Wahrscheinlichkeit, dass
    ein Alert mit DIESEM Wortschatz historisch eher ein Treffer war. None bei leerem
    Text (keine auswertbaren Woerter) - dann liefert das Modell keine Meinung."""
    words = _tokenize(text)
    if not words:
        return None
    hit_docs = model["hit_docs"]
    miss_docs = model["miss_docs"]
    total = hit_docs + miss_docs
    vocab_size = model["vocab_size"] or 1

    log_hit = math.log(hit_docs / total)
    log_miss = math.log(miss_docs / total)
    for w in words:
        hc = model["hit_counts"].get(w, 0)
        mc = model["miss_counts"].get(w, 0)
        # Laplace-Glaettung (+1): ein nie gesehenes Wort zieht die Wahrscheinlichkeit
        # nicht auf exakt 0/1.
        log_hit += math.log((hc + 1) / (hit_docs + vocab_size))
        log_miss += math.log((mc + 1) / (miss_docs + vocab_size))

    # log-sum-exp-Trick fuer eine numerisch stabile Umrechnung der Log-Wahrscheinlich-
    # keiten in eine normalisierte Wahrscheinlichkeit [0,1].
    m = max(log_hit, log_miss)
    p_hit = math.exp(log_hit - m)
    p_miss = math.exp(log_miss - m)
    return p_hit / (p_hit + p_miss)


def clear_cache() -> None:
    """Fuer Tests: erzwingt ein Neutraining beim naechsten get_model()-Aufruf."""
    _model_cache["model"] = None
    _model_cache["trained_at"] = 0.0
