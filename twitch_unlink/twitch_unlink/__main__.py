import sys

from twitch_unlink.cli import main as cli_main


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        from twitch_unlink.web.app import main as web_main

        web_main()
        return 0
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())