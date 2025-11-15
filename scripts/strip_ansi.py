#!/usr/bin/env python
"""
Strip ANSI escape codes from stdin and write to stdout.
Used to clean up plugin development server output for log files.
"""
import sys
import re

# Regex pattern to match ANSI escape sequences
ANSI_ESCAPE_PATTERN = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def strip_ansi(text):
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE_PATTERN.sub('', text)

def main():
    """Read from stdin, strip ANSI codes, write to stdout."""
    try:
        for line in sys.stdin:
            # Strip ANSI codes and write
            clean_line = strip_ansi(line)
            sys.stdout.write(clean_line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error in strip_ansi: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
