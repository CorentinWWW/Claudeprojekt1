"""Billiger, Claude-FREIER Relevanz-Vorfilter (erste Stufe des Trichters).

Seit der Verallgemeinerung (kein Personen-/Themenfilter mehr an den Quellen, siehe
news_gdelt.py/news_rss.py) kommt deutlich mehr Rohmaterial herein. Jeder neue Text
wuerde sonst einen echten Claude-Call kosten (und einen Slot des Tages-Kostendeckels
MAX_CLASSIFICATIONS_PER_DAY belegen) - der Deckel waere damit oft schon vormittags
ausgeschoepft. Dieser Vorfilter verwirft OFFENSICHTLICH nicht marktbewegende
Schlagzeilen (Listicles, Ratgeber/How-to, reine Personal-Finance-/Werbe-Clickbait),
BEVOR ein Call ausgegeben wird.

Bewusst KONSERVATIV (hohe Praezision statt hoher Trefferzahl): im Zweifel lieber
durchlassen. Ein faelschlich durchgelassener Grenzfall kostet nur einen Call und wird
danach sauber von Claude als nicht marktrelevant eingestuft; ein faelschlich
verworfenes echtes Ereignis waere dagegen fuer immer verloren. Deshalb ausschliesslich
eine Denylist eindeutiger Nicht-Ereignis-Muster - KEINE "muss ein Signalwort
enthalten"-Positivpflicht, die echte, schlicht formulierte Meldungen verwerfen koennte.
"""
import re

# Eindeutige Nicht-Ereignis-Muster (englischsprachig, da GDELT auf sourcelang:english
# eingeschraenkt ist und die RSS-Feeds ueberwiegend englisch sind). Jede Zeile ist ein
# Muster, bei dem eine reale, diskrete marktbewegende Nachricht praktisch ausgeschlossen
# ist - typische Ratgeber-/Listen-/Werbe-/Meinungs-Formate der Finanzportale.
_NOISE_PATTERN = re.compile(
    r"(?:"
    # Listicles: "5 stocks to watch", "3 ways to", "7 charts", "10 things"
    r"\b\d+\s+(?:stocks?|things|ways|reasons|tips|charts|etfs?|funds?|moves?|"
    r"lessons|mistakes|dividend stocks?)\b"
    # Ratgeber / How-to / Erklaerstuecke. Bewusst OHNE "here's why/how/what",
    # "what to know", "explainer"/"explained" und "credit card": diese Phrasen sind
    # ohne eigenes Themen-Anker auch der STANDARD-Schlagzeilenstil echter, zeitnaher
    # Finanznachrichten selbst (z.B. "Here's why Tesla stock plunged today", "Visa
    # hikes credit card fees, shares jump") - als Denylist-Muster ohne Themenbezug
    # haetten sie genau die Meldungen verworfen, die dieser Filter laut Docstring
    # niemals verwerfen darf. Die verbleibenden, spezifischeren Muster (z.B. "how to",
    # "401k", "retirement planning", "best N") fangen die zugehoerigen Nicht-Ereignis-
    # Beispiele weiterhin ab.
    r"|\bhow to\b|\bwhat to watch\b"
    r"|\bshould you\b|\bis it time to\b|\bguide to\b"
    r"|\ba beginner'?s guide\b|\beverything you need to know\b|\b401\(?k\)?\b|\broth ira\b"
    # Kauf-Empfehlungs-/Anlage-Clickbait der einschlaegigen Portale
    r"|\bmotley fool\b|\bzacks\b|\bstocks? to buy\b|\bstocks? to watch\b"
    r"|\bbest\s+\d+\b|\btop\s+\d+\b|\bworst\s+\d+\b|\bmy \d+ favorite\b"
    r"|\bis (?:it|now) a good time to (?:buy|sell)\b"
    # Meinung (nur eindeutige Kennzeichnungen, kein bloss vorkommendes "opinion")
    r"|\bop-ed\b"
    # Werbung / Promo / Personal Finance / Lifestyle
    r"|\bprime day\b|\bblack friday\b|\bcyber monday\b|\bcoupon\b|\bpromo code\b"
    r"|\bdiscount code\b|\bpersonal finance\b|\bbudgeting\b"
    r"|\bretirement (?:tips|savings|planning|account)\b|\bhoroscope\b|\brecipe\b"
    r"|\bhow much (?:you|to) (?:save|need)\b"
    r")",
    re.IGNORECASE,
)


def looks_market_relevant(text: str) -> bool:
    """True, wenn der Text KEIN eindeutiges Nicht-Ereignis-Muster enthaelt (also fuer
    eine (teure) Claude-Klassifikation in Frage kommt). Leerer/fehlender Text -> False
    (nichts zu klassifizieren)."""
    if not text or not text.strip():
        return False
    return _NOISE_PATTERN.search(text) is None
