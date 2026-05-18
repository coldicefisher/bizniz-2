"""Make ``python -m bizniz.perf_tests ...`` work."""
from bizniz.perf_tests.runner import main
import sys

if __name__ == "__main__":
    sys.exit(main())
