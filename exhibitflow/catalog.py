"""Museum catalog label selection from received editorial records."""


def select_label(records, object_id, language):
    """Return the latest matching editorial label, without historical reconciliation."""
    matching = [
        row for row in records
        if row["object_id"] == object_id and row["language"] == language
    ]
    if not matching:
        return None
    selected = max(matching, key=lambda row: (row["revision"], row["received_at"]))
    return {
        "object_id": object_id,
        "language": language,
        "title": selected["title"],
        "date_label": selected["date_label"],
        "source_id": selected["source_id"],
    }
