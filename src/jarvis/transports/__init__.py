"""Transports: the audio pipe of a session (shared protocol + the local mic/speaker).

Nothing is re-exported here; import from the modules themselves. Importing any
submodule executes this file, so a package whose members must be importable
independently re-exports nothing: `local_audio` needs `sounddevice`, which no
Linux host has, and it must not arrive on the back of `transports.base`.
"""
