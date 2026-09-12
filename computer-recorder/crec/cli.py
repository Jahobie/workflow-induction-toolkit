import argparse
import asyncio
from contextlib import contextmanager
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from crec import crec
from crec.observers import Screen

def parse_args():
    parser = argparse.ArgumentParser(description='Record desktop actions and screenshots')
    parser.add_argument(
        "--output-dir",
        type=str,
        default="~/Downloads/records",
        help="Directory for actions.db and screenshots",
    )
    parser.add_argument('--user-name', '-u', type=str, default="anonymous", help='The user name to use')
    parser.add_argument('--debug', '-d', action='store_true', help='Enable debug mode')
    
    # Scroll filtering options
    parser.add_argument('--scroll-debounce', type=float, default=0.5, 
                       help='Minimum interval between scroll screenshots (seconds, default: 0.5; Linux: 0 disables)')
    parser.add_argument('--scroll-min-distance', type=float, default=5.0,
                       help='macOS only: minimum pointer movement between retained scrolls (pixels, default: 5.0)')
    parser.add_argument('--scroll-max-frequency', type=int, default=10,
                       help='macOS only: maximum scroll events per second (default: 10)')
    parser.add_argument('--scroll-session-timeout', type=float, default=2.0,
                       help='macOS only: scroll session timeout (seconds, default: 2.0)')
    
    return parser.parse_args()

async def _main():
    args = parse_args()
    print(f"User Name: {args.user_name}")

    output_dir = os.path.abspath(
        os.path.expanduser(args.output_dir)
    )

    screen_observer = Screen(
        screenshots_dir=os.path.join(output_dir, "screenshots"),
        debug=args.debug,
        scroll_debounce_sec=args.scroll_debounce,
        scroll_min_distance=args.scroll_min_distance,
        scroll_max_frequency=args.scroll_max_frequency,
        scroll_session_timeout=args.scroll_session_timeout,
    )

    async with crec(
        args.user_name,
        screen_observer,
        data_directory=output_dir,
    ):
        await screen_observer.wait()

        if screen_observer.failure is not None:
            raise screen_observer.failure

@contextmanager
def _terminal_without_flow_control():
    """Keep Ctrl+S from suspending output; restore the caller's tty on exit."""
    if os.name != 'posix' or not sys.stdin.isatty():
        yield
        return
    import termios
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    recording = original.copy()
    recording[0] &= ~termios.IXON
    termios.tcsetattr(fd, termios.TCSANOW, recording)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)


def main():
    with _terminal_without_flow_control():
        try:
            asyncio.run(_main())
        except (RuntimeError, ImportError) as exc:
            print(f"Recording stopped: {exc}", file=sys.stderr)
            raise SystemExit(1) from None

if __name__ == '__main__':
    main()
