"""Dashboard widgets: cards that show live state.

`model` is the payload every widget produces, `registry` the types that can be placed,
`runtime` how a reading is taken and kept, and `builtin` the types that ship with the
manager. Importing the package registers them.
"""

from app.core.widgets import builtin as builtin  # registers the built-in types
