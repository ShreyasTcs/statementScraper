# POC V1 – Implementation Plan

## 1. Objective

Build a cost-effective TCS bill information extraction POC for two claim heads:

- **Travel Conveyance**
- **Food**

The system accepts PDF/image bills, extracts only the fields required for the selected claim head, validates the extracted values, calculates a transparent heuristic confidence score, and presents the result in an editable form beside the **actual uploaded document**.

POC V1 does **not** use an LLM or an ML model for extraction/confidence.

---

## 2. Target architecture

```text
User selects Claim Head
        |
        v
User uploads actual Bill
        |
        v
File Validation
        |
        v
Input Analysis
        |
        +----------------------------+
        |                            |
Text-based PDF                 Image / scanned PDF
        |                            |
Direct text extraction             OCR
        |                            |
        +-------------+--------------+
                      |
                      v
            Text + coordinates
                      |
                      v
              PP-StructureV3
          layout/structure analysis
                      |
                      v
           Claim-head-specific
              field extraction
                      |
                      v
                Validation
                      |
                      v
          Heuristic confidence score
                      |
                      v
             Structured JSON
                      |
                      v
     Actual uploaded bill + extracted form
                      |
                      v
             User review/edit
                      |
                      v
                   Save
```

---

## 3. Scope for POC V1

### Travel Conveyance fields

| Field | Source | Required |
|---|---|---|
| claim_head | User selection | Yes |
| from_location | Bill | Yes |
| to_location | Bill | Yes |
| bill_date | Bill | Yes |
| bill_no | Bill | Yes, if present |
| currency | Bill | Yes |
| bill_amount | Bill | Yes |
| remarks | System | Auto-fill `Travel Conveyance` |

### Food fields

| Field | Source | Required |
|---|---|---|
| claim_head | User selection | Yes |
| hotel_name | Bill | Yes |
| bill_date | Bill | Yes |
| bill_no | Bill | Yes, if present |
| currency | Bill | Yes |
| bill_amount | Bill | Yes |
| remarks | System | Auto-fill `Food` |

### Important business rule

Currency is **extracted as shown on the bill**. Do not force INR and do not mark a foreign currency as invalid merely because the claim UI may have INR as a dropdown/default.

Example:

```json
{
  "currency": "AED",
  "bill_amount": 240.00
}
```

---

## 4. Document rendering requirement

The UI must display the **exact file uploaded by the user**.

Do not generate or recreate a bill.

### PDF

Use a PDF viewer/rendering component to display the original uploaded PDF, including multi-page navigation.

### JPG/JPEG/PNG

Display the original uploaded image.

### Source highlighting

When technically possible, connect an extracted field to its source coordinates:

```text
User clicks Bill Amount
        |
        v
Viewer moves to source page
        |
        v
Original source region is highlighted
```

The application UI is only the surrounding shell; the document itself remains the uploaded file.

---

## 5. Recommended technology stack

### Frontend

- React
- TypeScript
- Tailwind CSS
- shadcn/ui or equivalent component library
- PDF.js/react-pdf for PDF rendering
- standard image rendering for JPG/JPEG/PNG

### Backend

- Python
- FastAPI
- Pydantic
- SQLAlchemy
- SQLite for POC persistence

### Document processing

- PyMuPDF (`fitz`) or `pdfplumber` for text-based PDFs
- PaddleOCR for OCR
- PP-StructureV3 for document structure/layout
- OpenCV/Pillow only where image preprocessing is useful

---

## 6. Development phases

### Phase 1 – Project setup

- React frontend.
- FastAPI backend.
- SQLite + SQLAlchemy.
- Local upload storage.
- Configuration/logging.
- Basic error handling.

### Phase 2 – Upload and file analysis

Implement:

- PDF/JPG/JPEG/PNG validation.
- File size/type validation.
- File hash generation.
- Page count detection.
- PDF text-layer detection.
- Image metadata detection.

Output:

```json
{
  "file_type": "pdf",
  "has_text_layer": true,
  "page_count": 2,
  "requires_ocr": false
}
```

### Phase 3 – Text/OCR layer

- Direct PDF extraction when selectable text exists.
- OCR for images and scanned PDFs.
- Preserve page, text and bounding-box information where available.
- Normalize all outputs into one internal representation.

### Phase 4 – PP-StructureV3 integration

Wrap PP-StructureV3 behind a `StructureAnalyzer` interface.

Do not expose model-specific data structures to the UI.

### Phase 5 – Claim-head schemas

Create:

```text
travel_conveyance
food
```

Each schema defines:

- target fields;
- aliases/anchors;
- expected data type;
- normalization;
- validation;
- confidence signals.

### Phase 6 – Extraction engine

Use:

1. exact/near-exact anchor matching;
2. alias matching;
3. nearby text;
4. bounding-box proximity;
5. document structure;
6. normalization;
7. candidate ranking.

Extract **only required claim fields**.

### Phase 7 – Validation engine

Use deterministic field-level checks.

Examples:

- date is parseable;
- amount is numeric;
- currency is recognized when present;
- source is sufficiently close to an expected anchor;
- conflicting candidates are surfaced;
- missing bill number remains `null`.

### Phase 8 – Heuristic confidence

Create a transparent V1 score from observable evidence.

Example signals:

```text
field_found
anchor_match
format_valid
spatial_proximity
unique_candidate
source_quality
```

Start with configurable weighted scoring.

### Phase 9 – UI

Build:

- upload;
- processing;
- actual document viewer;
- extracted-field form;
- confidence indicators;
- source highlighting;
- editing;
- save.

### Phase 10 – Testing

Test:

- text PDFs;
- scanned PDFs;
- JPG/PNG;
- one/multi-page documents;
- missing values;
- multiple amount candidates;
- different currencies;
- different layouts;
- Travel/Food.

---

## 7. Suggested project structure

```text
tcs-bill-poc-v1/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   ├── core/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   │   ├── file_service.py
│   │   │   ├── pdf_service.py
│   │   │   ├── ocr_service.py
│   │   │   ├── structure_service.py
│   │   │   ├── extraction_service.py
│   │   │   ├── validation_service.py
│   │   │   └── confidence_service.py
│   │   ├── rules/
│   │   │   ├── common.py
│   │   │   ├── travel.py
│   │   │   └── food.py
│   │   └── db/
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── features/
│   │   │   ├── upload/
│   │   │   └── claim-review/
│   │   ├── services/
│   │   └── types/
│   └── tests/
├── data/
│   ├── uploads/
│   └── processed/
├── docs/
└── README.md
```

---

## 8. Definition of Done

POC V1 is successful when:

- user selects Travel Conveyance or Food;
- user uploads an actual bill;
- text PDFs avoid unnecessary OCR;
- images/scanned PDFs use OCR;
- PP-StructureV3 is integrated;
- only required claim fields are extracted;
- actual uploaded bill is visible beside the result;
- extracted fields are editable;
- source regions can be highlighted where coordinates exist;
- validation is visible;
- heuristic confidence is understandable;
- structured JSON can be generated;
- core processing runs locally without paid LLM/document APIs.
