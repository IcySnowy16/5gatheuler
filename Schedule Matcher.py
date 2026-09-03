"""Launcher kept for backwards compatibility - the bot now lives in the
schedule_matcher package. Run:  python "Schedule Matcher.py"  """

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schedule_matcher.bot import main

if __name__ == "__main__":
    main()
