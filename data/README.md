# Local data

This directory is the default local landing zone for databases, raw responses, and derived datasets. Its contents are ignored by Git because market data is large, mutable, and often subject to redistribution constraints.

The collector stores raw API envelopes append-only under `raw/<exchange>/<dataset>/YYYY/MM/DD/`. Each envelope includes its request, response, receipt time, schema version, and payload digest. Curated records belong in the normalized database. Neither form should be committed to the source repository.
