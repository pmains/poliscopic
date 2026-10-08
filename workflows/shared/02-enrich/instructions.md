# Enrich — Attach Full Text and Supporting Documents

This step runs after classification. For each classified item, the system
attaches the full agenda item text and any supporting documents (staff
reports, agenda packets, ordinance text) from the database.

## What Happens

1. Reads the classified topic files from the previous step
2. For each item, looks up the full agenda item text from the meeting database
3. Fetches supporting documents (agenda packets, staff reports, exhibits)
   linked to each agenda item
4. Writes enriched topic files with all available source material

## Output Format

Each topic file gets enriched with:

```json
{
  "topic": "housing",
  "date": "2026-07-21",
  "items": [
    {
      "body": "apache-junction-cc",
      "meeting_id": "1411212",
      "meeting_date": "2026-07-07",
      "meeting_type": "Regular Meeting",
      "meeting_title": "City Council Meeting",
      "agenda_item_number": "7.7.26",
      "agenda_item_title": "CCPH - Development Impact Fees",
      "jurisdiction_name": "Apache Junction",
      "relevance": "high",
      "reason": "Directly concerns housing development fee policy",
      "agenda_item_text": "Full text of the agenda item from the database...",
      "source_url": "https://...",
      "supporting_documents": [
        {
          "title": "Staff Report - Development Impact Fees",
          "url": "https://...",
          "text": "First 5000 chars of document text..."
        }
      ]
    }
  ]
}
```

## Notes

- This step is deterministic (no LLM call). It simply enriches classified
  items with database content.
- Not all items will have supporting documents — some agenda items are
  informational or have no attached packets.
- Items without supporting documents can still have useful full agenda
  item text for the summarize step.
