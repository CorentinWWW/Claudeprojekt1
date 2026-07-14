"""Fuehrt alle Testdateien in tests/ aus - jede in einem EIGENEN Prozess.

Bewusst kein pytest: die Tests arbeiten mit importlib.reload()-Ketten und
DB_PATH-Umgebungsvariablen, die sich bei gemeinsamem Interpreter-Zustand
(wie pytest ihn hat) gegenseitig beeinflussen wuerden. Ein Prozess pro Datei
isoliert das vollstaendig, ohne zusaetzliche Abhaengigkeit.

Aufruf (aus dem Repo-Root oder von ueberall):
    python tests/run_tests.py
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent


def main() -> int:
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    if not test_files:
        print("Keine Testdateien gefunden.")
        return 1

    failed: list[str] = []
    for test_file in test_files:
        print(f"\n{'=' * 60}\n{test_file.name}\n{'=' * 60}")
        result = subprocess.run(
            [sys.executable, str(test_file)],
            cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            failed.append(test_file.name)

    print(f"\n{'=' * 60}")
    if failed:
        print(f"FEHLGESCHLAGEN ({len(failed)}/{len(test_files)}): {', '.join(failed)}")
        return 1
    print(f"Alle {len(test_files)} Testdateien erfolgreich.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
