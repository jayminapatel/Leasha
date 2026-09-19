"""Handlers extracted from `app/ui/shell.py`'s `MainWindow`.

Layer: L5

`SettingsController` owns what happens when a Settings control changes;
`IndexController` owns an index run's lifecycle and the work around it. Both
are built by `MainWindow.__init__` and reach the window's state through it -
see the module docstrings for why `MainWindow` keeps a same-named method for
every handler that moved.
"""
