"""Keep tkinter importable when the build interpreter cannot initialize Tcl."""


def pre_find_module_path(hook_api):
    """Use Python's standard-library tkinter package without probing a GUI."""
