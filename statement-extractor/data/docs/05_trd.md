# TRD – TCS Bill Information Extraction POC V1

## 1. Technical objective

Implement a local-first document extraction application with:

- deterministic text acquisition;
- conditional OCR;
- PP-StructureV3 layout/structure analysis;
- claim-head-specific extraction;
- deterministic validation;
- heuristic confidence;
- actual document viewing;
- editable results;
- structured JSON.

---

## 2. High-level component architecture

```text
                         FRONTEND
                            |
                            | HTTP/JSON + multipart
                            v
                         FASTAPI
                            |
          +-----------------+------------------+
          |                 |                  |
          v                 v                  v
    File Service       Claim Service      Result Service
          |
          v
   Input Analyzer
          |
    +-----+----------------+
    |                      |
    v                      v
PDF text reader        OCR service
    |                      |
    +----------+-----------+
               |
               v
     Normalized document model
               |
               v
      PP-StructureV3 adapter
               |
               v
       Extraction Engine
               |
               v
       Validation Engine
               |
               v
    Confidence Engine (V1)
               |
               v
          SQLite DB

FRONTEND ALSO RECEIVES:
original uploaded file / controlled preview endpoint
               |
               v
        Actual Document Viewer
```

---

## 3. Frontend technical requirements

### Core modules

```text
UploadPage
ClaimHeadSelector
ProcessingStatus
DocumentViewer
PdfPageThumbnails
ZoomControls
ExtractedFieldsPanel
ExtractedField
ConfidenceBadge
ConfidenceDetails
ValidationMessage
ReviewSummary
SaveClaimButton
```

### Document viewer

The viewer must render the **actual uploaded document**.

Do not generate a synthetic bill.

For PDFs:

- render original PDF pages;
- support page navigation;
- support zoom/fit-to-width.

For images:

- display original image;
- support zoom if necessary.

For source highlighting:

- overlay highlight on original document using source coordinates.

---

## 4. Backend API

### POST `/api/v1/documents`

Upload:

```text
file
claim_head
```

### GET `/api/v1/documents/{document_id}`

Returns:

- metadata;
- status;
- processing information.

### GET `/api/v1/documents/{document_id}/preview`

Controlled endpoint for the frontend to render the original uploaded file.

### GET `/api/v1/documents/{document_id}/result`

Returns extraction, validation and confidence.

### PATCH `/api/v1/documents/{document_id}/result`

Accept user corrections.

### POST `/api/v1/documents/{document_id}/save`

Save finalized claim.

### GET `/api/v1/claims/{claim_id}`

Retrieve saved claim.

---

## 5. Processing interfaces

### Text reader

```python
class TextReader:
    def can_read(self, file_path: str) -> bool: ...
    def read(self, file_path: str) -> "DocumentContent": ...
```

### OCR

```python
class OCRService:
    def extract(self, image_path: str) -> "DocumentContent": ...
```

### Structure

```python
class StructureAnalyzer:
    def analyze(self, content: "DocumentContent") -> "StructuredDocument": ...
```

### Extractor

```python
class ClaimExtractor:
    def extract(
        self,
        structured_document,
        claim_head
    ) -> "ExtractionResult": ...
```

### Validator

```python
class ValidationService:
    def validate(self, extraction_result) -> "ValidationResult": ...
```

### Confidence

```python
class ConfidenceService:
    def score(self, extraction_result, validation_result):
        ...
```

---

## 6. Normalized document model

```json
{
  "document_id": "uuid",
  "pages": [
    {
      "page_number": 1,
      "width": 1200,
      "height": 1600,
      "blocks": [
        {
          "id": "block-1",
          "text": "Total ₹1,069.50",
          "bbox": [100, 500, 700, 560],
          "source": "pdf_text",
          "confidence": null
        }
      ]
    }
  ]
}
```

For OCR:

```json
{
  "source": "ocr",
  "confidence": 0.97
}
```

The `bbox` values are also used by the frontend for source highlighting on the original document.

---

## 7. Extraction design

### Field configuration

```python
FIELD_CONFIG = {
    "bill_amount": {
        "anchors": [
            "total",
            "grand total",
            "amount payable",
            "net amount"
        ],
        "type": "money"
    }
}
```

For each candidate:

1. find anchor;
2. locate nearby values;
3. use layout/bounding boxes;
4. normalize;
5. rank candidate;
6. return candidate + evidence.

---

## 8. Travel extraction rules

### From Location

Potential anchors:

- From
- Pickup
- Pick-up
- Starting point
- Origin

### To Location

Potential anchors:

- To
- Drop
- Drop-off
- Destination

### Bill Date

Potential anchors:

- Bill Date
- Date
- Receipt Date
- Invoice Date
- Transaction Date

Context must reduce the chance of choosing unrelated dates.

### Bill No.

Potential anchors:

- Bill No
- Invoice No
- Receipt No
- Order No
- Order Number
- Reference No

Return `null` when absent.

### Currency

Normalize recognizable codes/symbols while preserving actual currency.

```text
₹ / INR → INR
AED → AED
```

Do not enforce INR.

### Bill Amount

Prefer final totals over component fees.

---

## 9. Food extraction rules

### Hotel Name

Use document structure and contextual anchors such as:

- Hotel
- Restaurant
- Merchant
- Vendor
- Bill From

Avoid simply selecting the first organization-like text.

### Bill Date

Same date strategy as Travel.

### Bill No.

Same identifier strategy as Travel.

### Currency

Preserve actual document currency.

### Bill Amount

Prefer final payable/total amount.

---

## 10. Validation

Validation is deterministic.

### Date

- parseable;
- valid calendar date;
- plausible;
- contextually relevant.

### Amount

- numeric;
- reasonable decimal handling;
- context suggests total/payable amount.

### Bill number

- plausible identifier;
- not obviously a phone number/date.

### Location

- meaningful textual/address-like content.

### Hotel name

- meaningful merchant/hotel-like value.

---

## 11. Heuristic confidence

Suggested V1 evidence signals:

```text
candidate_found
anchor_match
format_valid
spatial_proximity
unique_candidate
source_quality
```

Example output:

```json
{
  "confidence": 0.94,
  "status": "high"
}
```

Thresholds:

```text
0.90–1.00 → high
0.70–0.89 → review
below 0.70 → needs_review
```

These are configurable and should be calibrated during testing.

---

## 12. Actual document viewer integration

The frontend should receive a controlled document URL/reference, not a local filesystem path.

Example:

```text
GET /api/v1/documents/{document_id}/preview
```

The browser renders the original file.

For source highlighting:

```text
field source bbox
      ↓
map normalized coordinates to viewer coordinates
      ↓
draw highlight overlay
```

The underlying document must remain untouched.

---

## 13. File storage

Local POC:

```text
data/uploads/{document_id}/original.ext
data/processed/{document_id}/
```

The original file is retained for preview and traceability.

Do not expose direct filesystem paths.

---

## 14. Security

- validate type/size;
- generate server-side names;
- prevent path traversal;
- don't log raw bill content;
- protect preview endpoints;
- avoid unnecessary PII logs.

---

## 15. Testing

Test complete paths:

```text
Text PDF
→ direct text
→ structure
→ extraction
→ validation
→ UI preview

JPG
→ OCR
→ structure
→ extraction
→ validation
→ UI preview

Scanned PDF
→ OCR
→ structure
→ extraction
→ validation
→ UI preview
```

Also test:

- missing bill number;
- multiple totals;
- foreign currencies;
- poor images;
- multi-page bills;
- source highlighting;
- manual edits.

---

## 16. Extensibility

New claim heads should primarily add:

```text
schema
+ fields
+ extraction aliases/rules
+ validation rules
```

The document viewer, processing pipeline and APIs should remain reusable.
