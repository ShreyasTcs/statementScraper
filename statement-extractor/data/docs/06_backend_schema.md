# Backend Schema – TCS Bill Information Extraction POC V1

## 1. Database

Use:

- SQLite
- SQLAlchemy ORM

The schema should remain portable to PostgreSQL later.

---

## 2. Entity relationship

```text
documents
    |
    | 1
    |----< processing_runs
    |
    | 1
    |----< claim_extractions
                  |
                  | 1
                  |----< extraction_fields
                                   |
                                   | 1
                                   |----< manual_edits
```

---

## 3. `documents`

Stores uploaded-file metadata.

| Column | Type | Notes |
|---|---|---|
| id | UUID/string | Primary key |
| original_filename | string | Original client filename |
| stored_filename | string | Server-generated |
| file_type | string | PDF/JPG/JPEG/PNG |
| mime_type | string | Detected MIME |
| file_size_bytes | integer | Size |
| sha256 | string | File identity |
| page_count | integer | PDF page count |
| has_text_layer | boolean | PDF analysis |
| requires_ocr | boolean | Derived |
| preview_path/key | string | Controlled storage reference |
| created_at | datetime | Creation |

The original uploaded file is retained because the frontend must display it as the source document.

---

## 4. `processing_runs`

| Column | Type | Notes |
|---|---|---|
| id | UUID/string | Primary key |
| document_id | FK | Document |
| claim_head | string | Travel/Food |
| status | string | queued/processing/completed/failed |
| text_source | string | pdf_text/ocr |
| ocr_used | boolean | Yes/No |
| structure_model | string | PP-StructureV3 |
| started_at | datetime | Start |
| completed_at | datetime | End |
| error_code | string nullable | Safe code |
| error_message | string nullable | Safe detail |

---

## 5. `claim_extractions`

| Column | Type | Notes |
|---|---|---|
| id | UUID/string | Primary key |
| document_id | FK | Document |
| processing_run_id | FK | Processing run |
| claim_head | string | Travel/Food |
| status | string | extracted/reviewed/saved |
| overall_confidence | float nullable | Aggregate V1 indicator |
| created_at | datetime | Creation |
| updated_at | datetime | Change |

---

## 6. `extraction_fields`

Stores each field independently.

| Column | Type | Notes |
|---|---|---|
| id | UUID/string | Primary key |
| claim_extraction_id | FK | Parent |
| field_name | string | bill_amount, bill_date, etc. |
| extracted_value | text nullable | Original extraction |
| normalized_value | text nullable | Normalized |
| value_type | string | text/date/money/currency |
| confidence | float nullable | 0–1 |
| confidence_status | string | high/review/needs_review |
| validation_status | string | valid/invalid/not_found/review |
| source_page | integer nullable | Page |
| source_bbox | JSON nullable | Source coordinates |
| source_text | text nullable | Evidence |
| anchor_text | text nullable | Matched label |
| candidate_count | integer nullable | Candidate count |
| was_manually_edited | boolean | Edit flag |
| created_at | datetime | Creation |
| updated_at | datetime | Change |

The source fields support highlighting on the **actual uploaded document**.

---

## 7. `manual_edits`

| Column | Type | Notes |
|---|---|---|
| id | UUID/string | Primary key |
| extraction_field_id | FK | Field |
| old_value | text nullable | Previous |
| new_value | text nullable | Corrected |
| edited_by | string | User |
| edited_at | datetime | Time |

---

## 8. Travel JSON

```json
{
  "claim_head": "Travel Conveyance",
  "from_location": "string|null",
  "to_location": "string|null",
  "bill_date": "YYYY-MM-DD|null",
  "bill_no": "string|null",
  "currency": "string|null",
  "bill_amount": 0.0,
  "remarks": "Travel Conveyance",
  "confidence": {
    "from_location": 0.0,
    "to_location": 0.0,
    "bill_date": 0.0,
    "bill_no": 0.0,
    "currency": 0.0,
    "bill_amount": 0.0
  }
}
```

---

## 9. Food JSON

```json
{
  "claim_head": "Food",
  "hotel_name": "string|null",
  "bill_date": "YYYY-MM-DD|null",
  "bill_no": "string|null",
  "currency": "string|null",
  "bill_amount": 0.0,
  "remarks": "Food",
  "confidence": {
    "hotel_name": 0.0,
    "bill_date": 0.0,
    "bill_no": 0.0,
    "currency": 0.0,
    "bill_amount": 0.0
  }
}
```

---

## 10. API response

```json
{
  "document": {
    "id": "doc_123",
    "filename": "receipt.pdf",
    "page_count": 2,
    "ocr_used": false,
    "preview_url": "/api/v1/documents/doc_123/preview"
  },
  "claim": {
    "claim_head": "Travel Conveyance"
  },
  "fields": [
    {
      "name": "bill_amount",
      "value": "1069.50",
      "confidence": 0.96,
      "status": "high",
      "validation": "valid",
      "source": {
        "page": 1,
        "bbox": [100, 300, 600, 360],
        "text": "Total ₹1,069.50"
      }
    }
  ],
  "remarks": "Travel Conveyance"
}
```

---

## 11. Field status

Use:

```text
high
review
needs_review
not_found
invalid
```

Interpretation:

- `high`: strong evidence.
- `review`: usable extraction but verify.
- `needs_review`: weak/ambiguous.
- `not_found`: no reliable value.
- `invalid`: candidate failed validation.

---

## 12. Evidence storage

Store enough evidence to support:

- UI source highlighting;
- debugging;
- manual verification;
- error analysis;
- POC V2 dataset preparation.

Example:

```json
{
  "field": "bill_amount",
  "source_page": 1,
  "anchor_text": "Total",
  "source_text": "Total ₹1,069.50",
  "source_bbox": [100, 300, 600, 360]
}
```

---

## 13. Raw document handling

The `documents` record refers to the actual uploaded document stored under an internal identifier.

The frontend must never receive arbitrary filesystem paths.

Use a controlled preview/download endpoint.

---

## 14. POC V1 schema principles

1. Preserve the original uploaded document.
2. Store extraction separately from normalized values.
3. Store field-level confidence.
4. Store validation result.
5. Store source evidence.
6. Store manual edits.
7. Preserve actual document currency.
8. Allow missing fields with `null`.
9. Keep claim-head fields schema-controlled.
10. Keep vendor/model integrations behind service abstractions.
11. Retain evidence useful for POC V2.
