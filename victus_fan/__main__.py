import sys

from .cli import main

# Abfrage nötig: unter Windows importieren Hilfsprozesse (multiprocessing,
# z. B. für "victus-fan sensors") dieses Modul erneut.
if __name__ == "__main__":
    sys.exit(main())
