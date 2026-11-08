"""Stable identity of the single public POI resource."""

import uuid

UNIFIED_POI_ID = uuid.UUID("f9a13db2-664e-5240-a237-4efbdf3f8eb0")
UNIFIED_POI_NAME = "全部 POI"

# This identifies old source-package metadata. It is kept for provenance but
# excluded from the public spatial-dataset catalog after unification.
POI_SOURCE_SQL = """(shape_type IN (1,11) AND (
    fields @> '[{"name":"id"},{"name":"name"},{"name":"type"},{"name":"adcode"}]'::jsonb
    OR fields @> '[{"name":"POI_ID"},{"name":"NAME"},{"name":"TYPE"},{"name":"ADCODE"}]'::jsonb
))"""
