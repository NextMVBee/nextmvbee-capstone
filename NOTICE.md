# Notice

This repository is published for the review of a course submission: the final capstone project of a course on autonomous agent systems. It is public so that the course's reviewers can read the code, run the review path and check the evidence. It is not an open-source release.

## Rights

Copyright (c) 2026 Rozi Qzamil. All rights reserved.

All rights in the author's code, texts and brand are reserved. This covers every file of this repository that the author wrote: the source code, the role instructions under `prompts/`, the contracts under `schemas/`, the tests, the documents, the evaluation records, the delivered sample documents, the name NextMVBee with its marks, and the name and marks of the author's own project shown in the samples.

No licence is granted. You may read this repository and run its review path to review the course submission. Any other use, including copying, modifying, distributing, or using the code or the texts in a product or a service, needs the author's written permission.

The repository is provided as it is, without warranty of any kind.

## Third-party components

This repository contains no third-party source code. The installation step downloads third-party packages from the Python package index, as pinned in `requirements.lock.txt`. Each remains under its own licence, and nothing in this notice changes those licences.

The lock file is the release's own file, unchanged, so it pins every package the full application uses: 81 packages. The code in this repository imports four of them directly.

| Package | Version | Licence, as declared in the package's metadata |
|---|---|---|
| langgraph | 1.2.13 | MIT |
| openai | 3.24.0 | Apache-2.0 |
| pydantic | 2.13.5 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |

The other 77 pinned packages are installed with them. Some are dependencies of the four above. The rest serve parts of the application that are not in this repository: the web interface, the sign-in, the document renderer and the search provider's client. Their declared licences, grouped:

| Licence, as declared | Packages |
|---|---|
| MIT | aiosqlite, annotated-doc, annotated-types, anyio, attrs, brotli, charset-normalizer, exa-py, fastapi, fonttools, h11, iniconfig, jiter, jsonschema, jsonschema-specifications, langchain-core, langchain-protocol, langgraph-checkpoint, langgraph-checkpoint-sqlite, langgraph-prebuilt, langgraph-sdk, langsmith, pluggy, pydantic_core, pyee, PyJWT, pytest, python-docx, PyYAML, referencing, rpds-py, truststore, typing-inspection, urllib3, workos |
| MIT-0 | cffi |
| MIT-CMU | Pillow |
| MIT and PSF-2.0 | greenlet |
| MIT or Apache-2.0 | ormsgpack, sniffio |
| MIT and Apache-2.0, both listed | sqlite-vec |
| BSD-3-Clause (or "BSD License", "Modified BSD License") | click, httpcore, httpcore2, httpx2, idna, Jinja2, jsonpatch, jsonpointer, lxml, MarkupSafe, pycparser, pypdf, python-dotenv, sse-starlette, starlette, uuid_utils, uvicorn, websockets, zstandard |
| BSD-2-Clause | Pygments, xxhash |
| Apache-2.0 | distro, opentelemetry-api, requests, requests-toolbelt, tenacity |
| Apache-2.0 or BSD-2-Clause | packaging |
| Apache-2.0 or BSD-3-Clause | cryptography |
| BSD-3-Clause and Apache-2.0, with the licences of its bundled dependencies | pypdfium2 |
| MPL-2.0 | certifi, pikepdf |
| MPL-2.0 and (Apache-2.0 or MIT) | orjson |
| PSF-2.0 | typing_extensions |
| LGPL-2.1-only | pycountry |
| GNU AGPL 3.0 or a commercial licence of its publisher (dual licensed) | PyMuPDF |
| Not declared in the index metadata; see the package's own licence file | playwright |

The licence names are those each package declares for its pinned version in the Python package index, read on 15 November 2026. The lock file has not changed since. The authoritative text is the licence file each package ships.

## Vendor and product names

Vendor and product names in the code, the tests, the evaluation records and the sample documents belong to their owners. They are named because the system researches public web pages. The quotes of web pages in the test fixtures and in the sample documents are short excerpts of public pages, kept with their addresses as evidence records.
