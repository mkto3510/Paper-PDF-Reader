"""Entry point for the source and frozen versions."""

if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()

    from paper_reader.window import main

    raise SystemExit(main())
