# PRD – TCS Bill Information Extraction POC V1

## 1. Product overview

### Product name

**TCS Claim Extractor – POC V1**

### Purpose

Reduce manual claim entry by extracting required information from employee-uploaded Travel Conveyance and Food bills.

The system shows the **actual uploaded bill** beside the extracted, editable claim fields so the user can quickly verify and correct the result.

---

## 2. Problem statement

Employees submit bills in different formats and layouts. Relevant information may appear under different labels or on different pages.

The system should extract only fields required for the selected claim head and ignore unrelated document content.

---

## 3. Scope

Claim heads:

1. Travel Conveyance
2. Food

---

## 4. Functional requirements

### FR-01 Claim head

User selects Travel Conveyance or Food before extraction.

### FR-02 Upload

Support:

- PDF
- JPG
- JPEG
- PNG

### FR-03 Document rendering

After upload, the review screen must display the **original uploaded file**.

The application must not recreate or generate a bill representation.

### FR-04 Document processing

- Detect usable PDF text.
- Use direct extraction when possible.
- Use OCR for images/scanned PDFs.
- Perform layout/structure analysis.

### FR-05 Travel extraction

Extract:

- From Location
- To Location
- Bill Date
- Bill No.
- Currency
- Bill Amount

Auto-fill:

- Remarks = `Travel Conveyance`

### FR-06 Food extraction

Extract:

- Hotel Name
- Bill Date
- Bill No.
- Currency
- Bill Amount

Auto-fill:

- Remarks = `Food`

### FR-07 Currency

Preserve the currency found on the bill. No automatic conversion in POC V1.

### FR-08 Missing values

Use `null`/Not found when no reliable value is available.

### FR-09 Validation

Perform deterministic field-level validation.

### FR-10 Confidence

Show a human-readable heuristic confidence indicator for each extracted field.

### FR-11 Source verification

Where source coordinates are available, selecting a field should highlight the corresponding area on the actual uploaded document.

### FR-12 Editing

All extracted fields should be editable.

### FR-13 Save

User can save the reviewed claim result.

### FR-14 JSON

Generate structured JSON containing the finalized claim data.

---

## 5. User experience requirements

The main review experience should be a single desktop screen:

```text
Actual Bill Viewer | Extracted Claim Form
```

Requirements:

- minimal page-level scrolling;
- internal document scrolling;
- sticky extraction panel;
- sticky save action;
- clear confidence badges;
- clear missing-field state;
- visible source highlighting where possible.

---

## 6. User stories

### US-01

As an employee, I want to select a claim head so the system knows which fields to extract.

### US-02

As an employee, I want to upload my real bill so the system can extract claim information.

### US-03

As an employee, I want to see my original bill next to the extracted values so I can verify them quickly.

### US-04

As an employee, I want to understand which values may need review.

### US-05

As an employee, I want to edit incorrect or missing values.

### US-06

As an evaluator/developer, I want structured JSON and extraction evidence for testing and later POC V2 work.

---

## 7. Acceptance criteria

### Travel

When Travel Conveyance is selected and a bill is uploaded:

- required Travel fields are attempted;
- remarks is automatically `Travel Conveyance`;
- currency reflects the bill;
- missing values are not fabricated;
- results are editable;
- confidence/status is visible.

### Food

When Food is selected:

- required Food fields are attempted;
- remarks is automatically `Food`;
- currency reflects the bill;
- missing values are not fabricated;
- results are editable;
- confidence/status is visible.

### Document viewer

- exact uploaded file is displayed;
- multi-page PDFs support page navigation;
- zoom/fit controls work;
- source highlighting does not modify the source document.

---

## 8. Non-functional requirements

### Cost

Use local/open-source processing in POC V1.

### Explainability

The user can understand confidence status and source evidence.

### Extensibility

New claim heads should be schema-driven.

### Reliability

One failed field must not prevent successful fields from being returned.

### Security

Treat uploaded documents and extracted information as sensitive business data.

---

## 9. Out of scope

- LLM extraction;
- ML confidence model;
- automatic currency conversion;
- all eight claim heads;
- automated claim approval;
- accounting posting;
- production SSO;
- production distributed processing;
- policy decisions about whether a claim should be approved.

---

## 10. Success metrics

Track:

- field-level extraction accuracy;
- not-found rate;
- false extraction rate;
- manual correction rate;
- OCR usage rate;
- processing time;
- confidence vs actual correctness.

The results become the evidence for deciding how POC V2 should be built.
