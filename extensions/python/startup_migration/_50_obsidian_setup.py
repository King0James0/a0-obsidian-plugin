from helpers.extension import Extension
from usr.plugins.obsidian.helpers import setup


class ObsidianSetup(Extension):
    """Runs at framework startup. Ensures Obsidian is installed, configured, and running headless."""

    def execute(self, **kwargs):
        setup.ensure()
