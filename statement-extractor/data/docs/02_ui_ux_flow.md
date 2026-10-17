# POC V1 – UI/UX Flow

## 1. UX goal

Create a modern enterprise expense application with:

- clean professional visual design;
- low cognitive load;
- minimal scrolling;
- obvious next action;
- editable extraction results;
- **actual uploaded bill visible beside extracted data**.

The main workflow should center around one review screen.

---

## 2. Critical product/UI rule

### Do NOT generate or recreate bill designs.

The application must never visually manufacture a bill/invoice/receipt.

The left side is a **document viewer**.

It dynamically renders:

- the actual uploaded PDF;
- the actual uploaded JPG/JPEG/PNG;
- all pages of a multi-page PDF.

The bill's content, logo, typography, colors and layout come entirely from the uploaded file.

Banani/UI design tools should only create the viewer shell and placeholder frame.

---

## 3. Primary desktop layout

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ TCS Claim Extractor                  Travel Conveyance   Help   Profile   │
├───────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│ ┌────────────────────────────────────┐ ┌────────────────────────────────┐ │
│ │                                    │ │ Extracted Details              │ │
│ │         ACTUAL UPLOADED BILL       │ │                                │ │
│ │                                    │ │ Claim Head [Travel Convey.]   │ │
│ │       PDF / IMAGE VIEWER           │ │                                │ │
│ │                                    │ │ From Location [____________]  │ │
│ │                                    │ │ To Location   [____________]  │ │
│ │                                    │ │ Bill Date     [____________]  │ │
│ │                                    │ │ Bill No.      [____________]  │ │
│ │                                    │ │ Currency      [____________]  │ │
│ │                                    │ │ Bill Amount   [____________]  │ │
│ │                                    │ │ Remarks       [Travel...]     │ │
│ │                                    │ │                                │ │
│ │   [original uploaded document]     │ │ ✓ High confidence · 96%       │ │
│ │                                    │ │                                │ │
│ │  ◀ 1 / 2 ▶    − 100% +            │ │ [Review Issues] [Save Claim]  │ │
│ └────────────────────────────────────┘ └────────────────────────────────┘ │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
```

Recommended width:

- viewer: 55–60%;
- extraction panel: 40–45%.

No overall page scrolling on normal desktop screens.

---

## 4. Screen 1 – Upload

```text
TCS Claim Extractor

Submit a Bill
Upload a bill and we'll extract the information required for your claim.

Claim Head
[ Travel Conveyance ▼ ]

┌───────────────────────────────────────────────┐
│                                               │
│              Drop your bill here              │
│                      or                       │
│                [ Browse files ]               │
│                                               │
│              PDF, JPG, JPEG, PNG              │
└───────────────────────────────────────────────┘

Selected file:
receipt.pdf                         [Change] [Remove]

[Extract Bill]
```

Do not create a fake bill preview here.

---

## 5. Screen 2 – Processing

Use user-friendly labels:

```text
✓ Uploading bill
✓ Reading document
● Analyzing bill layout
○ Extracting required information
○ Validating details
```

Message:

> "We're extracting the information needed for your claim."

Do not expose OCR/PP-StructureV3/model terminology to normal users.

---

## 6. Screen 3 – Main review screen

### Left: actual uploaded document

The viewer should include:

- original uploaded PDF/image;
- page thumbnails for multi-page PDFs;
- zoom;
- fit-to-width;
- page navigation;
- internal document scrolling.

Do not redesign, OCR-preview, or reconstruct the document visually.

### Right: extracted details form

Show only the selected claim-head fields.

---

## 7. Travel form

```text
Extracted Details

Claim Head
[ Travel Conveyance ]

From Location
[ extracted value ]

To Location
[ extracted value ]

Bill Date
[ extracted value ]

Bill No.
[ extracted value ]

Currency
[ extracted value ]

Bill Amount
[ extracted value ]

Remarks
[ Travel Conveyance ]
```

---

## 8. Food form

```text
Extracted Details

Claim Head
[ Food ]

Hotel Name
[ extracted value ]

Bill Date
[ extracted value ]

Bill No.
[ extracted value ]

Currency
[ extracted value ]

Bill Amount
[ extracted value ]

Remarks
[ Food ]
```

All extracted values should be editable.

---

## 9. Source highlighting

When the user clicks a field:

```text
Bill Amount
     ↓
viewer navigates to source page
     ↓
original document region highlighted
```

The highlighted region must be overlaid on the actual uploaded document, not replaced with a generated bill.

---

## 10. Confidence UX

Use:

### High

`✓ High confidence · 90–100%`

### Medium

`! Review suggested · 70–89%`

### Low

`⚠ Needs review · below 70%`

Example:

```text
Bill Amount
[ ₹1,069.50 ]

✓ High confidence · 96%
```

The number should be secondary to the human-readable status.

---

## 11. Confidence explanation

Click the confidence badge to open a small popover:

```text
Why 94%?

✓ Clear field label found
✓ Value format is valid
✓ Value is close to the label
✓ Only one strong candidate found

Source
Page 1 · near "Total"
```

Do not expose technical feature weights.

---

## 12. Missing field

```text
Bill No.
[                                  ]

⚠ Not found in document
You can enter it manually.
```

The user must never see fabricated values.

---

## 13. Manual edit

After editing:

```text
Bill Amount
[ ₹1,099.50 ]

● Manually edited
```

Manual editing should feel intentional, not like an error.

---

## 14. Review summary

At the top of the extraction panel:

```text
6 fields extracted
5 high confidence
1 needs review
```

or:

```text
Ready for review
```

For low-confidence results:

```text
Review required
```

---

## 15. Action area

Sticky action area:

```text
[Review Issues]     [Save Claim]
```

Optional:

```text
[Reset Changes]
```

Save should remain easily accessible.

---

## 16. Multi-page behavior

Use:

- page thumbnails;
- central document viewer;
- internal viewer scrolling.

Only the document viewer should scroll for long bills.

The extraction panel should remain sticky.

---

## 17. Responsive behavior

### Desktop

Split-screen.

### Tablet

Approximately 50/50.

### Mobile

Use:

```text
[ Bill ] [ Extracted Data ]
```

---

## 18. Visual style

Use:

- white/light neutral background;
- dark neutral typography;
- one restrained corporate accent;
- subtle borders;
- light shadows;
- 8–12px radius;
- compact spacing;
- modern sans-serif;
- strong hierarchy.

Avoid:

- excessive gradients;
- fake bill visuals;
- decorative invoice templates;
- excessive cards;
- dashboard clutter;
- heavy animations.

The product should feel like premium enterprise software.

---

## 19. Core UX principle

The user should experience:

> **See the real bill → see what the system extracted → understand confidence → verify/edit → save.**
