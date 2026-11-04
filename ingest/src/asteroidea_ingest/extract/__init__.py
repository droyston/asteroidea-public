"""Extractors. Not imported by the commit Lambda: they need pynwb.

The commit image installs no `extract` dependency (pynwb, h5py, remfile), and
`commit.py`, `import_file.py` and the other handlers never import this package. The
`.dockerignore` also drops this directory from the commit image's build context.
"""
