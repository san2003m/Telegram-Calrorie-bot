# Third-Party Rights and Notices

The Apache License 2.0 in this repository applies to the original source code,
documentation, and configuration files in this repository. It does not relicense
third-party software, external services, product data, product images, trademarks,
or user-supplied content.

This file is informational and is not a substitute for reviewing the applicable
license or service terms before redistribution or commercial operation.

## Python dependencies

This repository does not vendor the source code of its Python dependencies. They are
downloaded separately by package-management tools and remain under their respective
licenses. The direct runtime dependencies declared at the time of this notice include:

| Dependency | License family |
|---|---|
| aiogram | MIT |
| aiosqlite | MIT |
| asyncpg | Apache-2.0 |
| FastAPI | MIT |
| HTTPX | BSD-3-Clause |
| OpenAI Python SDK | Apache-2.0 |
| Pillow | MIT-CMU |
| Pydantic and pydantic-settings | MIT |
| SQLAlchemy | MIT |
| Uvicorn | BSD-3-Clause |
| ZXing-C++ | Apache-2.0 |

Development dependencies are likewise installed separately and retain their own
licenses. Transitive dependency versions and license terms can change. Anyone
redistributing a prebuilt wheel or container image should generate and review an
up-to-date software bill of materials and retain all notices required by the bundled
dependencies and base image.

## Open Food Facts

The application can retrieve and cache product information from Open Food Facts at
runtime. No Open Food Facts database dump or product image is included in this source
repository.

- The Open Food Facts database is available under the Open Database License (ODbL).
- Individual database contents are available under the Database Contents License.
- Product images are available under a Creative Commons Attribution-ShareAlike license
  and may also contain logos, packaging artwork, trademarks, or other third-party rights.

Runtime users and distributors are responsible for the attribution, notice,
share-alike, and other obligations that apply to their particular reuse. See:

- https://openfoodfacts.github.io/openfoodfacts-server/api/
- https://world.openfoodfacts.org/terms-of-use

## External services and user content

Telegram and OpenAI are external services. Their names, APIs, SDKs, and services are
subject to their respective terms, policies, and trademark rights. The Apache-2.0
license for this repository does not grant rights to those services or marks.

Photos, labels, product names, brands, barcodes, and other content supplied by users or
retrieved at runtime remain subject to any applicable copyright, database, privacy,
publicity, and trademark rights. Such runtime content is not licensed by this project.
