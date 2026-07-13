# Components and license justification

This document explains the licensing choices for **jeicob** and the licenses
of its dependencies.

## Project license: MIT

jeicob is released under the **MIT License**. This is a permissive license
that allows anyone to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the software, provided the original copyright notice and
permission notice are included in all copies or substantial portions of the
software.

### Why MIT?

- **Maximum adoption**: MIT is the most widely used open-source license.
  It imposes minimal restrictions, making it easy for others to use, integrate,
  and build upon this project.
- **No copyleft obligations**: Unlike GPL, MIT does not require derivative works
  to be released under the same license. This is important for a prototype
  intended for future integration into a larger system.
- **No patent clause**: MIT does not include an explicit patent grant. For a
  small research prototype with no patent implications, this is acceptable.
  If patent concerns arise in the future, the license could be changed for
  future releases.

## Dependency licenses

All dependencies are listed below with their licenses. Every dependency uses
an OSI-approved license compatible with MIT.

| Package | License | Compatibility |
|---------|---------|---------------|
| `anthropic` | MIT | Compatible |
| `ollama` | MIT | Compatible |
| `pydantic` | MIT | Compatible |
| `pypdf` | BSD-3-Clause | Compatible |
| `python-dotenv` | BSD-3-Clause | Compatible |
| `qdrant-client` | Apache-2.0 | Compatible |
| `fastembed` | Apache-2.0 | Compatible |
| `rich` | MIT | Compatible |

### Notes on specific licenses

- **Apache-2.0** (qdrant-client, fastembed): Includes an explicit patent grant
  and protection against patent claims. Compatible with MIT.
- **BSD-3-Clause** (pypdf, python-dotenv): Very similar to MIT with an additional
  clause prohibiting use of the authors' names for endorsement. Compatible with MIT.

## Conclusion

All dependencies use permissive licenses (MIT, BSD-3-Clause, or Apache-2.0).
There are no copyleft (GPL/AGPL) dependencies that would impose additional
distribution requirements on jeicob.
