# Conditional OCR and layout correction

Use this workflow only after detecting a concrete defect that affects navigation, extraction, or meaning. Do not load it for normal clean output.

## Trigger conditions

Correct a derived Markdown page only when at least one condition holds:

- A contents entry is split, reordered, merged with another entry, or assigned an implausible page number.
- Multi-column text is interleaved and changes reading order.
- OCR creates obvious substitutions in a part number, technical term, formula, unit, sign, decimal point, or numeric value.
- A table loses headers, shifts columns, drops footnotes, or makes min/typ/max ambiguous.
- Broken headings or page boundaries prevent reliable search and navigation.

Ignore harmless line wrapping, spacing, punctuation style, or cosmetic Markdown differences.

## Minimal repair procedure

1. Identify the smallest affected page or passage.
2. Open or render only the corresponding original PDF page. For a contents defect, inspect only the affected contents pages and any page needed to confirm numbering.
3. Save an audit copy of the affected generated Markdown under `<pdf-stem>.mineru/raw/reviewed/`; never overwrite the original raw selection.
4. Correct only what the source page clearly supports, then apply that minimal correction to the top-level `<pdf-stem>.md`. Preserve technical wording, numbers, units, table footnotes, and page provenance.
5. Add a short HTML comment at the corrected block:

```markdown
<!-- ai-reviewed: pdf_page=12; reason=toc-layout; confidence=high -->
```

6. If the correction changes a factual value, formula, part number, or unit, require direct source-page confirmation. If confirmation is uncertain, leave the text unchanged and report the ambiguity.
7. Continue reading the top-level Markdown. Do not review unrelated pages or expose the audit copy as a second normal reading document.

For a badly damaged page, prefer rerunning only that page with the `accurate` profile before manual repair. Stop after one targeted retry.
