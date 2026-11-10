Source: https://github.com/tevador/hashwx

Revision: `74b567a31276a4c5d7cb232a5b7639467f49f961` (the version vendored by Cap).

The `include/`, `src/`, `CMakeLists.txt`, and `LICENSE` files are upstream files; PupulinBot removes trailing whitespace from include/hashwx.h only.
Copyright (c) 2020–2026 tevador. Distributed under LGPL-3.0-or-later; see LICENSE.

Only the `hashwx` shared-library target is built by this project's `cap-build` command.
Generated binaries are cached outside the source tree. No CUDA/GPU implementation is included.
