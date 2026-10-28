"""Launch the local desktop application: python main.py."""
import argparse

from review_ui import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", help="Open an existing candidate report for review")
    main(report_path=parser.parse_args().report)
