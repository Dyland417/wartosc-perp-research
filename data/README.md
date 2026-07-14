# Local data

This directory is the default local landing zone for databases, raw responses, and derived datasets. Its contents are ignored by Git because market data is large, mutable, and often subject to redistribution constraints.

Raw API payloads should eventually be stored append-only under a date/exchange/dataset partition. Curated tables belong in the normalized database. Neither should be committed to the source repository.

