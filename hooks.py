"""Framework runtime hooks (run in /opt/venv-a0), called by the plugin installer/uninstaller."""


def install():
    """Install Obsidian, seed config, launch headless — without waiting for a restart."""
    from usr.plugins.obsidian.helpers import setup

    setup.ensure()


def uninstall():
    """Stop Obsidian, remove the wrapper, uninstall Obsidian if we installed it (vault preserved)."""
    from usr.plugins.obsidian.helpers import setup

    setup.cleanup()
