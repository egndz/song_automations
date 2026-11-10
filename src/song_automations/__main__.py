"""Lets the command line run as python -m song_automations, which the schedule uses."""

from song_automations.cli import app

if __name__ == "__main__":
    app(prog_name="song-automations")
