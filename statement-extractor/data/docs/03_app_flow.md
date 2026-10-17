# POC V1 – Application Flow

## 1. End-to-end app flow

```text
START
  |
  v
Dashboard / Upload
  |
  v
Select Claim Head
  |
  +---------------------------+
  |                           |
  v                           v
Travel Conveyance             Food
  |                           |
  +-------------+-------------+
                |
                v
          Upload Actual Bill
                |
                v
          File Validation
                |
                v
         Document Analysis
                |
        +-------+--------+
        |                |
        v                v
Text PDF            Image / Scan
        |                |
Direct extraction       OCR
        |                |
        +-------+--------+
                |
                v
        Text + Coordinates
                |
                v
        PP-StructureV3
                |
                v
      Select target schema
                |
        +-------+--------+
        |                |
        v                v
 Travel extraction   Food extraction
        |                |
        +-------+--------+
                |
                v
             Validation
                |
                v
       Heuristic confidence
                |
                v
         Review screen
                |
      +---------+---------+
      |                   |
      v                   v
  Edit values           Accept
      |                   |
      +---------+---------+
                |
                v
          Save result
                |
                v
              JSON
                |
                v
               END
```

---

## 2. Key UI behavior

At the review stage:

```text
LEFT:
Actual uploaded document

RIGHT:
Claim-specific extracted form
```

The application does not generate a substitute bill.

---

## 3. Upload API

```http
POST /api/v1/documents
Content-Type: multipart/form-data
```

Payload:

- `file`
- `claim_head`

Backend returns:

```json
{
  "document_id": "uuid",
  "status": "processing",
  "claim_head": "TRAVEL_CONVEYANCE"
}
```

---

## 4. File analysis

Checks:

- extension;
- MIME type;
- size;
- PDF text layer;
- page count.

Decision:

```text
PDF + usable text
    → direct extraction

PDF + no usable text
    → render pages + OCR

JPG/JPEG/PNG
    → OCR
```

---

## 5. Normalized document representation

```json
{
  "pages": [
    {
      "page_number": 1,
      "blocks": [
        {
          "text": "Total ₹1,069.50",
          "bbox": [100, 200, 450, 240],
          "confidence": 0.98
        }
      ]
    }
  ]
}
```

This allows the UI to locate and highlight the source region in the **original uploaded document**.

---

## 6. Structure analysis

PP-StructureV3 analyzes document layout/structure.

Integration should remain behind:

```python
StructureAnalyzer
```

This preserves replaceability.

---

## 7. Claim-head schemas

### Travel

```text
from_location
to_location
bill_date
bill_no
currency
bill_amount
remarks
```

### Food

```text
hotel_name
bill_date
bill_no
currency
bill_amount
remarks
```

---

## 8. Targeted extraction

For each field:

```text
Field definition
   |
   +--> candidate anchors
   +--> expected value type
   +--> layout expectations
   +--> normalization
   +--> candidate ranking
```

Example:

```text
bill_amount
   |
   +--> Total
   +--> Grand Total
   +--> Amount Payable
   +--> Net Amount
```

Candidate ranking uses:

- anchor proximity;
- spatial relationship;
- value type;
- section/region;
- uniqueness;
- structure.

---

## 9. Validation

Validation is field-level and deterministic.

Example:

```text
bill_date
  ✓ parseable
  ✓ plausible
  ✓ relevant context

bill_amount
  ✓ numeric
  ✓ relevant total context
  ✓ candidate is spatially appropriate

bill_no
  ! not found
```

---

## 10. Confidence

Each field gets a heuristic score.

Example:

```json
{
  "bill_amount": {
    "value": 1069.50,
    "confidence": 0.96,
    "status": "high"
  }
}
```

The user sees the score and status, not the internal algorithm.

---

## 11. Review and editing

The review screen displays:

```text
Actual uploaded bill | Editable extracted data
```

When a field is selected:

```text
field → source page → source highlight
```

When a value is edited, store:

```text
original_value
edited_value
edited_by
edited_at
```

---

## 12. Save

Final result includes:

- original extraction;
- current user value;
- validation status;
- confidence;
- metadata.

---

## 13. Error flow

### Unsupported file

Remain on upload screen and explain supported types.

### Extraction failure

Keep actual uploaded document visible; show required fields as `Not found`; permit manual entry.

### Low confidence

Show `Needs review` and source highlight when available.

### Multiple candidate conflict

Do not silently choose. Show `Review suggested` and expose candidate/source context.

---

## 14. Future extension

POC V2:

```text
POC V1 evidence/features
        ↓
ground truth
        ↓
ML confidence model
        ↓
data-driven confidence
```

Future optional fallback:

```text
low-confidence ambiguous field
        ↓
LLM semantic fallback
```

LLM is not part of default V1.
