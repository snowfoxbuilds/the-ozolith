"""`python -m theozolith_nodedaemon` — the update command re-execs this."""

import sys

from theozolith_nodedaemon.daemon import main

sys.exit(main())
