"""Engine integration: GTP subprocess, analysis parsing, position sync.

This package is mostly independent of the rest of the UI. The pieces are:

* :mod:`coords`     — GTP vertex <-> :class:`Point` conversion.
* :mod:`analysis`   — parse a ``kata-analyze`` output line into structured data.
* :mod:`position`   — build a self-contained :class:`AnalysisRequest` from the
  game model (run on the GUI thread, since it reads the SGF tree).
* :mod:`gtp`        — :class:`GtpEngine`: the subprocess + worker thread that
  serialises commands, streams live analysis, and supports a paused console.
* :mod:`config`     — saved engine commands (name + shell command), persisted.
* :mod:`controller` — :class:`AnalysisController`: glues the active tab's
  position to the engine and routes analysis back to the board overlay.

See ``docs/ENGINE.md`` for the full design (state model, sync algorithm,
latency/coalescing invariants).
"""
