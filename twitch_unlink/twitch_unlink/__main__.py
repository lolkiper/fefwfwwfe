import sys

from twitch_unlink.cli import main as cli_main


def main() -> int:
    if len(sys.argv) > 1:
        if sys.argv[1] == "web":
            from twitch_unlink.web.app import main as web_main

            web_main()
            return 0
        if sys.argv[1] == "gui":
            from twitch_unlink.gui.app import main as gui_main

            return gui_main()
    if getattr(sys, "frozen", False):
        from twitch_unlink.gui.app import main as gui_main

        return gui_main()
    if len(sys.argv) == 1:
        from twitch_unlink.gui.app import main as gui_main

        return gui_main()
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())