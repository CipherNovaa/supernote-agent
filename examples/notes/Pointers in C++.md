---
type: supernote
created: 2026-03-21 21:05
modified: 2026-03-21 21:05
status: captured
tags:
  - supernote
topics:
  - "[[Programming]]"
source: "[[Supernote/Raw/Pointers in C++.note]]"
---
# Pointers in C++

- A pointer **stores an address**, not a value
- `int* p = &x;` → `p` holds where `x` lives
- `*p` reads (or writes) the value at that address

## Watch out
- An uninitialised pointer points ==anywhere== — set it to `nullptr`
- Don't return the address of a local variable

***
Next: references vs pointers
<!-- sn:end -->
