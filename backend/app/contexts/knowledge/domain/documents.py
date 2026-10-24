MAX_UPLOAD = 20 * 1024 * 1024
DOCUMENT_FIELDS = (
    "id",
    "title",
    "filename",
    "role",
    "status",
    "mime_type",
    "size",
    "version",
    "case_id",
    "parent_id",
    "created_at",
    "source_url",
    "error",
    "family_id",
    "content_hash",
)
# Lists never materialize plaintext or embedding arrays from the manifest.
DOCUMENT_SUMMARY = (
    "owner_id," + ",".join(DOCUMENT_FIELDS) + ",tags,json_array_length(manifest,'$.chunks') chunk_count"
)
