# Send — Format as HTML Newsletter

Read the verified output from the previous step. Format the approved
items into an HTML email.

## Format

The system uses a deterministic template renderer — no LLM formatting.
The verified data is rendered through `workflows/templates/render.py`.

The renderer:
1. Reads `verify-result.json` for approval status and highlights summary
2. Reads the topic's report JSON for verified items
3. Groups items by jurisdiction with headers
4. Splits items into past and upcoming sections
5. Inserts the highlights summary from the verify step
6. Renders the HTML template from `workflows/templates/newsletter.html`

## Link Format

Every item must include a deep link:
```
https://poliscopic.com/meetings/{body}/{meeting_id}#item-{agenda_item_number}
```

## Output

Subject: Auto-generated from topic display name and date.
Return: {"html": "...", "subject": "...", "approved": true|false}
